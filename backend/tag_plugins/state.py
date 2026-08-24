# System tag plugin: state
# Protected by SYSTEM_TAGS in db.py — cannot be deleted or renamed via normal operations.
import re

DESCRIPTION = (
    "on_mutation（被任何涉及本概念的变更唤醒）：\n"
    "① 概念新增 state 标签时，检查其正文 (content) 是否包含 <state>...</state> 闭包块。\n"
    "若缺失或闭包内容过短，拒绝 (reject)，强制要求在闭包块内用自然语言写明状态判定/验证标准。\n"
    "audit：检查所有 state 标签概念，检测缺失 <state> 闭包块或内容过短的违规节点。"
)


def _extract_state_payload(concept) -> str | None:
    if not concept.content:
        return None
    match = re.search(r"<state>(.*?)</state>", concept.content, re.DOTALL | re.IGNORECASE)
    if match:
        payload = match.group(1).strip()
        if len(payload) >= 5:
            return payload
    return None


def on_mutation(ctx):
    cid = ctx.this.concept_id

    if not ctx.changed:
        return

    added_tags = ctx.changed.get("tags", {}).get("added", [])
    content_changed = "content" in ctx.changed

    should_check = any(t["tag"] == "state" for t in added_tags) or (
        content_changed and "state" in ctx.this.tags
    )

    if should_check:
        payload = _extract_state_payload(ctx.this)
        if not payload:
            ctx.reject(
                f"排异反应：概念 '{ctx.this.name}' (id={cid}) 被标记为 'state'，"
                f"但其正文 (content) 缺乏有效的 <state>...</state> 闭包块。\n"
                f"打上 'state' 标签的概念必须在正文内包含 <state> 闭包块，并用自然语言写明明确的状态判定与验证标准（如：<state>达成条件：... </state>）。"
            )


def audit_cluster(ctx):
    for c in ctx.cluster.concepts:
        payload = _extract_state_payload(c)
        if not payload:
            ctx.warn(
                f"[malformed] 状态节点 '{c.name}' (id={c.concept_id}) 正文缺乏有效的 <state>...</state> 闭包块，无法提取验证标准。"
            )
