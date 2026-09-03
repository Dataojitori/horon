"""
Horon Hook Gateway — Antigravity IDE 生命周期 Hooks 网关集成。

本模块是 Antigravity Agent 生命周期 (.agents/hooks.json) 与 Horon
神经效应器框架（sensor_hooks 感觉传入神经、tool_guards 运动传出效应器、attention routing）的物理桥梁。

支持的 IDE 生命周期事件 (Supported Events):
  - PreInvocation:  同步会话 (sync_session)、匹配用户消息感知器 (user_message sensors)。
  - PreToolUse:     匹配工具调用感知器 (tool_call sensors)、执行工具放行守卫物理拦截 (tool_guards allow/deny)。
  - PostToolUse:    匹配工具执行结果感知器 (tool_result sensors)、捕获并追踪执行报错。
  - PostInvocation: 从 transcript 中提取模型最新回复、匹配模型言论感知器 (model_message sensors，如寄生虫词汇、自虐认同或口头誓言)。
  - Stop:           循环终止检查与清理。

调用方式 (CLI Entry Point):
  python -m backend.hook_gateway <EventName>
  (通过 stdin 接收 Antigravity Hook 传入的 JSON Payload，通过 stdout 输出 Hook Decision JSON)
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path 中，支持模块化导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.db import HoronDB


logger = logging.getLogger("horon.hook_gateway")

HARNESS_PREFIX = "【Horon Harness】"


def wrap_harness_message(items: list[str]) -> str:
    """将一条或多条系统通知统一包装为带 【Horon Harness】 信头的广播块。

    确保信道物理隔离：所有注入 transcript 的系统广播由此函数统一封包，
    杜绝各业务模块散落定义或漏带信头导致的感知反射串台死循环。
    """
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        for line in item.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("- "):
                lines.append(line_str)
            else:
                lines.append(f"- {line_str}")
    if not lines:
        return ""
    return f"{HARNESS_PREFIX}\n" + "\n".join(lines)



def _read_stdin_payload() -> dict[str, Any]:
    """从标准输入 (stdin) 读取并解析 Antigravity IDE 传入的 Hook JSON 数据载荷。"""
    try:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
            if content:
                content = content.lstrip("\ufeff")
                return json.loads(content)
    except Exception as e:
        logger.debug(f"Failed to parse stdin payload: {e}")
    return {}


def _get_latest_transcript_entries(transcript_path: str | None, max_entries: int = 10) -> list[dict[str, Any]]:
    """从 transcript.jsonl 日志文件中倒序读取最新的会话步骤记录 (JSON Lines)。

    Antigravity IDE 的 transcript.jsonl 每一行都是一个 JSON 对象，记录了会话中的一个步骤（Step），
    例如用户输入 (USER_INPUT / USER_EXPLICIT) 或模型输出 (PLANNER_RESPONSE / MODEL)。
    本函数采用尾部块读取（Seek from end），从文件尾部向前解析提取最近发生的若干条事件，
    避免全量读取与切分大文件造成的内存占用与 I/O 阻塞。
    """
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists() or not p.is_file():
        return []

    entries: list[dict[str, Any]] = []
    chunk_size = 64 * 1024  # 64 KB 块大小

    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return []

            cursor = file_size
            buffer = b""

            while cursor > 0 and len(entries) < max_entries:
                read_size = min(chunk_size, cursor)
                cursor -= read_size
                f.seek(cursor)
                chunk = f.read(read_size)
                buffer = chunk + buffer

                # 按换行符分割已读取的字节流
                lines = buffer.split(b"\n")
                # 最前面的部分可能是未读完的半行，保留在 buffer 中继续向前读取拼装
                if cursor > 0:
                    buffer = lines[0]
                    complete_lines = lines[1:]
                else:
                    buffer = b""
                    complete_lines = lines

                # 倒序解析已完成的行
                for raw_line in reversed(complete_lines):
                    line_str = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        if isinstance(data, dict):
                            entries.append(data)
                            if len(entries) >= max_entries:
                                break
                    except Exception:
                        continue

            # 若已到文件头部且 buffer 仍有残留未处理行
            if buffer and len(entries) < max_entries:
                line_str = buffer.decode("utf-8", errors="replace").strip()
                if line_str:
                    try:
                        data = json.loads(line_str)
                        if isinstance(data, dict):
                            entries.append(data)
                    except Exception:
                        pass

        return entries
    except Exception as e:
        logger.debug(f"Failed to read transcript: {e}")
        return []


def sync_session(db: HoronDB, conversation_id: str) -> str | None:
    """确保 Horon 当前活跃会话严格对齐 IDE 传入的 conversation_id。

    工作逻辑：
    1. 校验 conversation_id 是否有效，若缺失或为空字符串则直接抛出 ValueError 拒绝执行；
    2. 查询数据库中的 current_session 表；
    3. 若 current_session 为空或与当前 conversation_id 不一致（新会话接入或切换会话）：
       - 调用 db.init_session() 重置会话；
       - 清空旧会话的 session 与 turn 临时传感器 (is_active = 0)；
       - 清空 active_chain_instances 时序状态机；
       - 重新执行 Kahn 拓扑排序求值并更新活跃会话 ID；
       - 返回标准格式的系统就绪提示字符串（不带信头前缀，由 hook 出口统一封装）。

    Returns:
        str | None: 若触发了新会话初始化/重置，返回系统就绪提示字符串；若已处于当前会话且无需重置返回 None。
    """
    if not conversation_id or not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("Hook payload 缺失有效的 conversationId，拒绝执行状态同步。")

    clean_id = conversation_id.strip()
    curr = db.conn.execute("SELECT session_id FROM current_session LIMIT 1").fetchone()
    if not curr or curr["session_id"] != clean_id:
        db.init_session(session_id=clean_id)
        return f"Horon 系统已初始化就绪（Session: `{clean_id}`）。"
    return None


def match_and_activate_sensors(
    db: HoronDB,
    event_type: str,
    text: str,
    tool_name: str | None = None,
) -> list[str]:
    """从数据库读取对应事件类型的感知钩子 (sensor_hooks)，与传入事实比对并点火激活。

    处理流程：
    1. 查询 sensor_hooks 与 concepts 表，读取所有注册在指定 event_type（如 user_message, tool_call 等）下的传感器规则；
    2. 针对每一条规则：
       - 若规则指定了 tool 且与当前 tool_name 不符，跳过；
       - 若规则包含 match_pattern（正则表达式），用其匹配输入文本 text；
       - 若正则命中，将该传感器 concept_id 加入待激活列表；
    3. 若有传感器被触发，调用 db.evaluate(activated_sensors=...)：
       - 将传感器状态更新为 is_active = 1；
       - 顺着概念图（AND / OR / CHAIN / INHIBIT）执行拓扑传播求值；
       - 收集本次传导中全图所有被激发节点的 on_fire notify 广播消息并返回。

    Returns:
        list[str]: 本次求值中全图触发的 notify 广播消息列表。
    """
    if not text and not tool_name:
        return []

    query = (
        "SELECT sh.id AS hook_id, sh.sensor_concept_id, sh.event_type, sh.tool, "
        "sh.match_pattern, c.name, c.is_active "
        "FROM sensor_hooks sh "
        "JOIN concepts c ON sh.sensor_concept_id = c.id "
        "WHERE sh.event_type = ?"
    )
    rows = db.conn.execute(query, (event_type,)).fetchall()
    activated = []

    for r in rows:
        # 若钩子绑定了特定工具名，则必须与当前调用的工具名匹配
        if r["tool"] and tool_name and r["tool"] != tool_name:
            continue
        if r["tool"] and not tool_name:
            continue

        pat = r["match_pattern"]
        if pat:
            try:
                if re.search(pat, text, re.IGNORECASE):
                    activated.append(r["sensor_concept_id"])
            except Exception as e:
                logger.debug(f"Regex error on hook #{r['hook_id']} pattern {pat!r}: {e}")
        else:
            # 若无正则但匹配到了目标工具
            if r["tool"] and tool_name and r["tool"] == tool_name:
                activated.append(r["sensor_concept_id"])

    messages: list[str] = []
    # 触发传感器入库点火并沿拓扑图重新求值，收集所有放电节点的 notify 动作
    if activated:
        eval_res = db.evaluate(activated_sensors=activated)
        for fa in eval_res.fired_actions:
            actions = fa.action if isinstance(fa.action, list) else [fa.action] if isinstance(fa.action, dict) else []
            for act in actions:
                if isinstance(act, dict) and "notify" in act and act["notify"]:
                    messages.append(f"- {act['notify']}")

    return messages


def check_tool_guards(db: HoronDB, tool_name: str, tool_args: dict[str, Any]) -> tuple[bool, str | None]:
    """检查工具调用是否受 tool_guards 守卫规则管辖，并判断是否具备放行权限。

    工作逻辑：
    1. 查询 tool_guards 表，查找所有拦截该 tool_name 的守卫概念 (guard concept)；
    2. 校验工具参数序列化字符串是否命中守卫规则的 args_pattern 正则；
    3. 若规则命中且该守卫概念处于未激活状态 (is_active == 0)：
       - 深入分析导致该守卫未激活的原因（缺少哪些前置 compose_members、或被哪些活跃的 inhibitor 抑制源锁死）；
       - 生成人类/模型可读的精确拦截原因（Permission Denied）；
       - 返回 (False, deny_reason)，直接物理拒绝该工具调用；
    4. 若所有命中的守卫皆已激活 (is_active == 1) 或未被任何守卫管辖，返回 (True, None) 放行。

    Returns:
        tuple[bool, str | None]: (是否放行, 拦截原因)。
    """
    args_json = json.dumps(tool_args, ensure_ascii=False)
    query = (
        "SELECT tg.id AS guard_rule_id, tg.guard_concept_id, tg.tool, tg.args_pattern, "
        "c.name, c.is_active "
        "FROM tool_guards tg "
        "JOIN concepts c ON tg.guard_concept_id = c.id "
        "WHERE tg.tool = ?"
    )
    guards = db.conn.execute(query, (tool_name,)).fetchall()

    for g in guards:
        pat = g["args_pattern"]
        if pat:
            try:
                if not re.search(pat, args_json, re.IGNORECASE):
                    continue  # 参数模式未命中，不受此条特定守卫规则约束
            except Exception:
                continue

        # 规则命中 -> 此工具调用受守卫概念 g['name'] 管辖
        if g["is_active"] == 0:
            cid = g["guard_concept_id"]
            try:
                # 溯源分析前置依赖与抑制链路
                members = db._get_compose_members(cid)
                unmet = [m.name for m in members if m.is_active == 0]
                inhs = db._get_inhibitions(cid, direction="incoming")
                active_inhs = [i.inhibitor_name for i in inhs if i.inhibitor_is_active == 1]

                if active_inhs:
                    reason = f"当前被活跃抑制源 ({', '.join(active_inhs)}) 强制锁死"
                elif unmet:
                    reason = f"当前缺少必要前置: {', '.join(unmet)}"
                elif not members:
                    reason = "[系统配置错误] 该守卫未配置任何前置条件（孤岛死锁）"
                else:
                    reason = "[系统内部Bug] 前置已全部满足但守卫电位未同步（求值状态不一致）"
            except Exception as e:
                reason = f"[系统内部异常] 守卫状态校验失败: {e}"

            return False, f"工具 '{tool_name}' 调用已被拦截：受守卫 '{g['name']}' 管辖，{reason}。"

    return True, None


def handle_pre_invocation(payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 PreInvocation 事件：在模型生成回复前执行会话同步、消费积压通知与输入感知。

    核心流程：
    1. 会话对齐 (sync_session)：确保数据库活跃会话与 IDE conversationId 一致；若是新会话，收集初始化知情通知；
    2. 消费积压通知 (pop_pending_notifications)：提取并清空上游（如工具调用 PreToolUse / PostToolUse / PostInvocation）产生的待发送通知；
    3. 感知用户输入 (user_message)：从 transcript 提取用户最新真实输入（跳过 Horon Harness 自身的系统广播），匹配并激活对应的感觉传感器；
    4. 收集本次 invocation 触发的所有广播通知，统一由 wrap_harness_message 单点打包注入。
    """
    conv_id = payload.get("conversationId")
    notifications: list[str] = []

    # 1. 会话对齐：若触发了新会话初始化/重置，收集系统就绪通知
    if init_msg := sync_session(db, conv_id):
        notifications.append(init_msg)

    # 2. 消费上游在工具执行或后置阶段暂存的待发送通知
    clean_id = conv_id.strip() if (conv_id and isinstance(conv_id, str)) else ""
    if clean_id:
        pending_msgs = db.pop_pending_notifications(clean_id)
        notifications.extend(pending_msgs)

    # 3. 从 transcript.jsonl 中提取用户最新发送的真实输入文本（过滤系统自身的广播）
    transcript_path = payload.get("transcriptPath")
    entries = _get_latest_transcript_entries(transcript_path, max_entries=5)
    user_text = ""
    for entry in entries:
        if entry.get("source") in ("USER_EXPLICIT", "USER") or entry.get("type") == "USER_INPUT":
            content = entry.get("content", "")
            # 统一过滤 Horon Harness 系统广播，确保传感器只匹配真正的人类输入
            if isinstance(content, str) and (content.startswith(HARNESS_PREFIX) or content.startswith("【Horon Harness")):
                continue
            user_text = content
            break

    # 4. 若抓取到用户输入，匹配 user_message 类型的感知器并点火，收集图谱传导产生的通知
    if user_text:
        if fired_msgs := match_and_activate_sensors(db, event_type="user_message", text=user_text):
            notifications.extend(fired_msgs)

    # 5. 通过 Antigravity Hook 规范的 injectSteps.userMessage 持久化注入通知（统一单点封包）
    if broadcast := wrap_harness_message(notifications):
        return {
            "injectSteps": [
                {"userMessage": broadcast}
            ]
        }
    return {}


def handle_pre_tool_use(payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 PreToolUse 事件：在工具实际执行前进行参数感知与守卫门禁拦截。

    核心流程：
    1. 会话对齐；
    2. 匹配 tool_call 感知器（例如检测 AI 是否试图读取敏感文件或执行特定命令）；
    3. 调用 check_tool_guards 校验守卫概念状态：未激活则拦截 (deny)，已激活则放行 (allow)；
    4. 若放行且传感器点火产生通知，暂存入 pending_notifications 队列供紧随其后的 PreInvocation 消费。
    """
    conv_id = payload.get("conversationId")
    sync_session(db, conv_id)
    clean_id = conv_id.strip() if (conv_id and isinstance(conv_id, str)) else ""

    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    tool_args = tool_call.get("args", {})
    args_json = json.dumps(tool_args, ensure_ascii=False)

    fired_msgs: list[str] = []
    # 1. 匹配 tool_call 类型的传感器（如试图读取敏感配置文件等）
    if tool_name:
        fired_msgs = match_and_activate_sensors(db, event_type="tool_call", text=args_json, tool_name=tool_name)

    # 2. 检查 tool_guards 工具守卫
    if tool_name:
        allowed, guard_reason = check_tool_guards(db, tool_name, tool_args)
        if not allowed:
            deny_items = [*fired_msgs, guard_reason] if guard_reason else fired_msgs
            return {
                "decision": "deny",
                "reason": wrap_harness_message(deny_items),
            }

    # 3. 若放行且点火产生通知，暂存入待发送队列供紧随其后的 PreInvocation 注入
    if fired_msgs and clean_id:
        db.push_pending_notifications(clean_id, fired_msgs)

    return {"decision": "allow"}


def handle_post_tool_use(payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 PostToolUse 事件：在工具执行完毕后感知执行结果与报错状态。

    核心流程：
    1. 会话对齐；
    2. 提取工具调用标识与执行结果（包含 output、result 与 error）；
    3. 匹配并点火 tool_result 传感器；
    4. 点火产生的通知暂存入 pending_notifications 队列供下一阶段消费。
    """
    conv_id = payload.get("conversationId")
    sync_session(db, conv_id)
    clean_id = conv_id.strip() if (conv_id and isinstance(conv_id, str)) else ""

    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name") or payload.get("toolName") or payload.get("tool", "")

    error = payload.get("error")
    result = payload.get("result") if payload.get("result") is not None else payload.get("output")
    if result is None:
        result = payload.get("response")

    contents: list[str] = []
    if error:
        contents.append(str(error))
    if result is not None:
        if isinstance(result, (dict, list)):
            contents.append(json.dumps(result, ensure_ascii=False))
        else:
            contents.append(str(result))

    combined_text = "\n".join(contents)
    if combined_text or tool_name:
        fired_msgs = match_and_activate_sensors(
            db, event_type="tool_result", text=combined_text, tool_name=tool_name or None
        )
        if fired_msgs and clean_id:
            db.push_pending_notifications(clean_id, fired_msgs)

    return {}


def handle_post_invocation(payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 PostInvocation 事件：在模型回复完成后感知模型自身输出，若触发规则立即打断并强制追问。

    核心流程：
    1. 会话对齐；
    2. 从 transcript 提取模型的最新回复文本（纯自然语言言论审查，工具调用由 PreToolUse 专一化管辖）；
    3. 匹配 model_message 感知器（例如检测是否出现寄生虫认同、过度自我贬低、空洞承诺等违规表达）；
    4. 若有新点火通知或积压通知，通过 injectSteps 注入 Harness 警告，并设置 terminationBehavior: "force_continue" 强制要求模型在当前轮次内立即修正。
    """
    conv_id = payload.get("conversationId")
    sync_session(db, conv_id)

    transcript_path = payload.get("transcriptPath")
    entries = _get_latest_transcript_entries(transcript_path, max_entries=5)

    model_text = ""
    for entry in entries:
        if entry.get("source") == "MODEL" or entry.get("type") == "PLANNER_RESPONSE":
            content = entry.get("content", "")
            if isinstance(content, str) and content.strip():
                model_text = content
                break

    fired_msgs: list[str] = []
    if model_text:
        fired_msgs = match_and_activate_sensors(db, event_type="model_message", text=model_text)

    if fired_msgs and (broadcast := wrap_harness_message(fired_msgs)):
        return {
            "injectSteps": [
                {"userMessage": broadcast}
            ],
            "terminationBehavior": "force_continue",
        }

    return {}


def handle_stop(payload: dict[str, Any], db: HoronDB) -> dict[str, Any]:
    """处理 Stop 事件：代理执行终止时的终态检查与积压通知兜底拦截。"""
    conv_id = payload.get("conversationId")
    sync_session(db, conv_id)
    clean_id = conv_id.strip() if (conv_id and isinstance(conv_id, str)) else ""

    # 兜底：若停机前仍有未被 PreInvocation 消费的积压通知，拦截停机并注入提示让模型继续处理
    if clean_id:
        pending = db.pop_pending_notifications(clean_id)
        if pending:
            broadcast = wrap_harness_message(pending)
            return {
                "decision": "continue",
                "reason": broadcast,
            }

    # 正式放行停机：单回合结束，熄灭 turn 传感器并重新求值全图
    db.turn_end()

    return {"decision": "allow"}


def dispatch_hook(event_name: str, payload: dict[str, Any], db: HoronDB | None = None) -> dict[str, Any]:
    """统一生命周期事件分发路由。根据 EventName 调用对应的处理函数并妥善管理数据库连接。"""
    should_close = False
    if db is None:
        db = HoronDB()
        should_close = True

    try:
        if event_name == "PreInvocation":
            return handle_pre_invocation(payload, db)
        elif event_name == "PreToolUse":
            return handle_pre_tool_use(payload, db)
        elif event_name == "PostToolUse":
            return handle_post_tool_use(payload, db)
        elif event_name == "PostInvocation":
            return handle_post_invocation(payload, db)
        elif event_name == "Stop":
            return handle_stop(payload, db)
        else:
            return {}
    finally:
        if should_close:
            db.close()


def main():
    """CLI 入口点：读取命令行参数与 stdin payload，执行分发并将决策 JSON 输出到 stdout。"""
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = _read_stdin_payload()

    if not event_name and "event" in payload:
        event_name = payload["event"]

    if not event_name:
        # 默认兜底放行
        print(json.dumps({}))
        return

    result = dispatch_hook(event_name, payload)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
