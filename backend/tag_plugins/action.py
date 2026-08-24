# System tag plugin: action
# Protected by SYSTEM_TAGS in db.py — cannot be deleted or renamed via normal operations.
import re

DESCRIPTION = (
    "on_mutation：概念新增 action 标签时，检查其正文 (content) 是否包含 <action>...</action> 闭包块。\n"
    "若缺失或闭包内容过短，拒绝 (reject)，强制要求在闭包块内用自然语言写明具体的执行步骤或操作指令。\n"
    "audit：检查所有 action 标签概念，检测缺失 <action> 闭包块或内容过短的违规节点。"
)


def _extract_action_payload(concept) -> str | None:
    if not concept.content:
        return None
    match = re.search(r"<action>(.*?)</action>", concept.content, re.DOTALL | re.IGNORECASE)
    if match:
        payload = match.group(1).strip()
        if len(payload) >= 3:
            return payload
    return None


def on_mutation(ctx):
    cid = ctx.this.concept_id

    if not ctx.changed:
        return

    added_tags = ctx.changed.get("tags", {}).get("added", [])
    content_changed = "content" in ctx.changed

    should_check = any(t["tag"] == "action" for t in added_tags) or (
        content_changed and "action" in ctx.this.tags
    )

    if should_check:
        payload = _extract_action_payload(ctx.this)
        if not payload:
            ctx.reject(
                f"排异反应：概念 '{ctx.this.name}' (id={cid}) 被标记为 'action'，"
                f"但其正文 (content) 缺乏有效的 <action>...</action> 闭包块。\n"
                f"打上 'action' 标签的概念必须在正文内包含 <action> 闭包块，并用自然语言写明具体的执行步骤或操作指令（如：<action>执行步骤：... </action>）。"
            )


def audit_cluster(ctx):
    for c in ctx.cluster.concepts:
        payload = _extract_action_payload(c)
        if not payload:
            ctx.warn(
                f"[malformed] 动作节点 '{c.name}' (id={c.concept_id}) 正文缺乏有效的 <action>...</action> 闭包块，无法提取执行步骤。"
            )
