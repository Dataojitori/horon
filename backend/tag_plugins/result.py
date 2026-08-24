# System tag plugin: result
# Protected by SYSTEM_TAGS in db.py — cannot be deleted or renamed via normal operations.

DESCRIPTION = (
    "on_mutation：概念新增 result 标签时，强制要求其必须同时拥有 state 标签（预期结果本质上是带验证标准的状态）。\n"
    "audit：检查所有 result 标签概念，检测是否缺失 state 标签。"
)


def on_mutation(ctx):
    cid = ctx.this.concept_id

    if not ctx.changed:
        return

    added_tags = ctx.changed.get("tags", {}).get("added", [])
    removed_tags = ctx.changed.get("tags", {}).get("removed", [])

    # Rule 1: Result must have state tag when adding result tag
    if any(t["tag"] == "result" for t in added_tags):
        if "state" not in ctx.this.tags:
            ctx.reject(
                f"排异反应：概念 '{ctx.this.name}' (id={cid}) 被标记为 'result'，但缺乏 'state' 标签。\n"
                f"只有 state 节点才能被打上 result 标签。预期结果本质上必须是一种拥有验证标准的状态。"
                f"请先给它打上 state 标签并补充 <state> 闭包验证标准。"
            )

    # Rule 2: Cannot remove state tag if concept still carries result tag
    if any(t["tag"] == "state" for t in removed_tags):
        if "result" in ctx.this.tags:
            ctx.reject(
                f"排异反应：概念 '{ctx.this.name}' (id={cid}) 持有 'result' 标签，不能移除其作为基础的 'state' 标签。\n"
                f"若要移除 'state'，必须先移除 'result' 标签。"
            )


def audit_cluster(ctx):
    """Result hygiene check: ensure all result concepts also have state tag."""
    for concept in ctx.cluster.concepts:
        if "state" not in concept.tags:
            ctx.warn(
                f"[malformed] 预期结果节点 '{concept.name}' (id={concept.concept_id}) 缺少 'state' 标签。"
            )
