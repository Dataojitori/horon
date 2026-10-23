"""
Horon 远程 HTTP 接口（第一阶段：单个库、一把 key）。

    POST /v1/cli
    请求头：X-API-Key: <key>   或   Authorization: Bearer <key>
    请求体：{"args": ["read_concept", "951"], "batch_text": "..."}
      args        就是 `python frontend/cli.py` 后面那串参数，一项一个，不要拼成一个字符串。
      batch_text  只在 args[0] == "batch" 时使用：每行一条命令的文本（格式同 batch 文件，省略
                  `python frontend/cli.py` 前缀）。batch 在这里不读文件、不读 stdin。
    返回：{"ok": true/false, "output": "...", "error": "..."}

    HTTP 状态：401 key 不对 / 没带；403 命令不在白名单或用了读写服务器文件的参数；
    422 请求体格式不对；503 服务端没配 key；200 命令真的跑了（成不成功看 ok）。

配置（仓库根目录 .env，或进程环境变量）：
    HORON_API_KEY   这把唯一的 API key。不设或为空 = 接口整体关闭（一律 503），不会放行。
    HORON_DB        照旧，决定打开哪个库。

这个接口和本地 CLI 的区别：
    - 每个请求在服务进程里直接调 frontend/cli.py 的 _build_parser() 和 _audited_dispatch()，
      不起子进程；每个请求开一个 HoronDB 连接，用完关闭。
    - 会话固定为 remote-api（审计日志、读取记录和本地的 devonly / 宿主会话分开）。
    - 不加载任何 Tag 插件（HoronDB(plugins_enabled=False)）。写入时不过插件检查，audit 不跑插件。
    - 不调用 hook 的 sense()，所以传感器、Jev 复核都不会因为这个接口的请求被求值
      （详见 set 配了电路属性后的行为：只落库，不求值正则、不问 Jev）。
    - 不走钩子。本地开新会话时做的完整性检查（缺向量后台补、电路组合违规提示）改在远程的
      login 里做，违规提示附在 login 输出末尾。
    - 只认白名单里的子命令，且不允许任何会读写服务器文件的参数。
"""
from __future__ import annotations

import hmac
import io
import logging
import os
import threading
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from .db import HoronDB
from .integrity import check_at_session_start
from frontend import cli as _cli

logger = logging.getLogger("horon.remote_api")

API_KEY_ENV = "HORON_API_KEY"
REMOTE_SESSION_ID = "remote-api"
REMOTE_ADAPTER = "remote-api"

# 子命令白名单：名单以外一律拒绝（reset、read_memory 等都不在里面）。
ALLOWED_COMMANDS = frozenset({
    "login", "read_concept", "search_keyword", "search_semantic", "list_concepts", "recent",
    "create_concept", "update", "set", "add", "delete", "place", "cut",
    "list_tags", "create_tag", "delete_tag", "remind", "inbox", "compile",
    "suggest_name", "audit", "batch",
})

# argparse 解析后的 dest → 命令行上的写法。这些参数会让服务器读写文件，一律拒绝。
# 按解析结果（Namespace）检查，不按字符串匹配：--content_file、--content-file=x 这些写法
# 都落到同一个 dest 上；而 `--append "--file"` 这种把字面文字当内容的合法请求也不会被误伤。
FILE_ARG_DESTS = {
    "content_file": "--content-file",
    "append_file": "--append-file",
    "old_file": "--old-file",
    "new_file": "--new-file",
    "file": "--file",
    "out": "--out",
}

MAX_BODY_BYTES = 1_000_000
MAX_ARGS = 200

# 同一时刻只让一个 /v1/cli 请求碰库：update --append 是“读旧值再写回”，并发会丢更新；
# 单用户阶段请求稀疏，排队的代价可以接受（慢命令如 audit --concept 会让后面的请求等它）。
_cli_lock = threading.Lock()

router = APIRouter()


class _RemoteError(Exception):
    status = 400


class _Rejected(_RemoteError):
    """策略拒绝：命令不在白名单、用了文件参数。"""
    status = 403


class _BadRequest(_RemoteError):
    """请求本身不合格（如 batch 没带 batch_text）。"""
    status = 422


class _CliRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: list[str]
    batch_text: str | None = None


def _reply(status: int, *, ok: bool = False, output: str = "", error: str = "") -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"ok": ok, "output": output, "error": error}, status_code=status, headers=headers)


# ── 鉴权 ─────────────────────────────────────────────────────────────────────

def _authenticate(request: Request) -> JSONResponse | None:
    """key 对返回 None，否则返回要发回去的拒绝响应。每次请求现读环境变量里的 key。"""
    expected = os.environ.get(API_KEY_ENV, "").strip()
    if not expected:
        return _reply(503, error=f"服务端没有配置 {API_KEY_ENV}，远程接口未开启。")
    bearer = ""
    authorization = request.headers.get("authorization", "")
    if authorization[:7].lower() == "bearer ":
        bearer = authorization[7:]
    supplied_x = request.headers.get("x-api-key", "")
    expected_bytes = expected.encode("utf-8")
    # 两个头都做常数时间比较，且不短路，不让“哪个头对了”从耗时里泄露。
    ok_x = hmac.compare_digest(supplied_x.strip().encode("utf-8"), expected_bytes)
    ok_bearer = hmac.compare_digest(bearer.strip().encode("utf-8"), expected_bytes)
    if ok_x | ok_bearer:
        return None
    return _reply(401, error="unauthorized：缺少或错误的 API key（X-API-Key 或 Authorization: Bearer）。")


# ── 白名单 ───────────────────────────────────────────────────────────────────

def _check_command_name(tokens: list[str]) -> None:
    """解析之前先查：第一项必须是白名单里的子命令（不接受放在子命令前面的任何选项）。"""
    if not tokens:
        raise _Rejected("args 不能为空：第一项是子命令名。")
    if tokens[0] not in ALLOWED_COMMANDS:
        raise _Rejected(f"命令 '{tokens[0]}' 不在白名单内。允许的子命令：{', '.join(sorted(ALLOWED_COMMANDS))}。")


def _check_no_file_args(ns) -> None:
    """解析之后查：任何会读写服务器文件的参数都拒绝。"""
    for dest, flag in FILE_ARG_DESTS.items():
        if getattr(ns, dest, None) is not None:
            raise _Rejected(
                f"远程接口不允许使用 {flag}（会读写服务器上的文件）。内容请直接写在请求里；"
                f"batch 的命令文本放在请求体的 batch_text 字段。")


def _check_parsed(ns) -> None:
    """已解析命令的完整策略检查，单条命令和 batch 的每一行都过这里。"""
    if ns.command not in ALLOWED_COMMANDS:  # 防御：上面按名字查过一遍，这里按解析结果再查一遍
        raise _Rejected(f"命令 '{ns.command}' 不在白名单内。")
    _check_no_file_args(ns)


class _Usage(Exception):
    """argparse 报了用法错误，或者打印了 --help。text 是它要输出的文字，code 是退出码。"""

    def __init__(self, code, text: str):
        self.code = code
        self.text = text


def _parse(parser, tokens: list[str]):
    """用 CLI 的 parser 解析一条命令；argparse 的输出收进缓冲区而不是写 stderr/stdout。"""
    buf = io.StringIO()
    reset_token = _cli._PARSER_OUTPUT.set(buf)
    try:
        return parser.parse_args(tokens)
    except SystemExit as e:
        raise _Usage(e.code, buf.getvalue()) from None
    finally:
        _cli._PARSER_OUTPUT.reset(reset_token)


def _usage_hint(text: str) -> str:
    if "unrecognized arguments" in text or "expected one argument" in text:
        text += ("\n[提示] args 是 CLI 参数数组，一个参数一项（不要把整条命令拼成一个字符串）。"
                 "值以 - 开头的内容请写成 --选项=值 的形式。")
    return text


# ── 执行 ─────────────────────────────────────────────────────────────────────

def _validate_batch(parser, batch_text: str) -> None:
    """batch_text 里每一行都先过一遍白名单和文件参数检查；任何一行不合格，整段都不执行。"""
    try:
        commands = _cli._parse_batch_commands(batch_text)
    except Exception as e:
        raise _BadRequest(f"batch_text 语法错误：{e}") from None
    if not commands:
        raise _BadRequest("batch_text 里没有命令。")
    for start_line, tokens, _raw in commands:
        try:
            _check_command_name(tokens)
            if tokens[0] == "batch":
                raise _Rejected("batch 里不能再嵌套 batch。")
            _check_parsed(_parse(parser, tokens))
        except _Usage as u:
            raise _BadRequest(f"batch_text 第 {start_line} 行参数错误：{_usage_hint(u.text).strip()}") from None
        except _Rejected as r:
            raise _Rejected(f"batch_text 第 {start_line} 行被拒绝：{r}") from None


def _open_db() -> HoronDB:
    db = HoronDB(snapshot_mode=True, session_id=REMOTE_SESSION_ID, plugins_enabled=False)
    try:
        db.touch_session(REMOTE_ADAPTER)  # 登记会话；已登记的只刷新最后活跃时间
    except Exception:
        db.close()
        raise
    return db


def _session_start_check(db: HoronDB) -> str:
    """远程的 login 相当于本地开新会话：跑同一个完整性检查（缺向量就在后台起补向量进程）。
    本地的电路违规提示走钩子广播，远程没有钩子，所以附在 login 输出末尾。出错只记日志。"""
    try:
        problems = check_at_session_start(db)
    except Exception:
        logger.exception("remote cli: session-start integrity check failed")
        return ""
    return f"\n【完整性检查】{problems}\n" if problems else ""


def _execute(args: list[str], batch_text: str | None) -> tuple[int, dict[str, Any]]:
    """跑一个请求，返回 (HTTP 状态, 响应 JSON)。同步函数，在线程池里跑。"""
    try:
        _check_command_name(args)
        if batch_text is not None and args[0] != "batch":
            raise _BadRequest("batch_text 只能和 batch 命令一起使用。")
        parser = _cli._build_parser()
        try:
            ns = _parse(parser, args)
        except _Usage as u:
            if u.code in (0, None):  # --help
                return 200, {"ok": True, "output": u.text, "error": ""}
            return 200, {"ok": False, "output": "", "error": _usage_hint(u.text).strip()}
        _check_parsed(ns)
        if ns.command == "batch":
            if batch_text is None:
                raise _BadRequest("batch 需要请求体里的 batch_text 字段（每行一条命令的文本）；这里不读文件也不读 stdin。")
            _validate_batch(parser, batch_text)
    except _RemoteError as e:
        return e.status, {"ok": False, "output": "", "error": str(e)}

    with _cli_lock:
        try:
            db = _open_db()
        except Exception as e:
            logger.exception("remote cli: failed to open database")
            return 500, {"ok": False, "output": "", "error": f"打开数据库失败：{type(e).__name__}: {e}"}
        try:
            if ns.command == "batch":
                buf = io.StringIO()
                reset_token = _cli._PARSER_OUTPUT.set(buf)
                try:
                    out, err = _cli._run_batch(parser, db, batch_text, ns.all, check=_check_parsed)
                finally:
                    _cli._PARSER_OUTPUT.reset(reset_token)
                error = (err or "").strip()
                if buf.getvalue():
                    error = (error + "\n" + buf.getvalue().strip()).strip()
                return 200, {"ok": err is None, "output": out, "error": error}
            result = _cli._audited_dispatch(ns, db)
            output = _cli._render(result)
            if ns.command == "login":
                output += _session_start_check(db)
            return 200, {"ok": True, "output": output, "error": ""}
        except ValueError as e:  # CLI 里“正常的失败”（找不到节点、参数不合法、拒绝写入等）
            return 200, {"ok": False, "output": "", "error": str(e)}
        except SystemExit as e:
            return 200, {"ok": False, "output": "", "error": f"命令异常退出（code={e.code}）"}
        except Exception as e:
            logger.exception("remote cli: unexpected error running %s", args[0])
            return 200, {"ok": False, "output": "", "error": f"内部错误：{type(e).__name__}: {e}"}
        finally:
            db.close()



# ── 路由 ─────────────────────────────────────────────────────────────────────

@router.post("/v1/cli")
async def post_cli(request: Request) -> JSONResponse:
    # 先鉴权，再读请求体：没有 key 的人连请求体格式对不对都得不到反馈。
    denied = _authenticate(request)
    if denied is not None:
        return denied
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _reply(413, error=f"请求体超过 {MAX_BODY_BYTES} 字节。")
    try:
        req = _CliRequest.model_validate_json(raw)
    except ValidationError as e:
        detail = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in e.errors())
        return _reply(422, error=f"请求体格式不对（需要 {{\"args\": [字符串, ...]}}）：{detail}")
    if len(req.args) > MAX_ARGS:
        return _reply(422, error=f"args 最多 {MAX_ARGS} 项。")
    status, payload = await run_in_threadpool(_execute, req.args, req.batch_text)
    logger.info("remote cli: %s -> %s ok=%s", req.args[0] if req.args else "(empty)", status, payload["ok"])
    return JSONResponse(payload, status_code=status)
