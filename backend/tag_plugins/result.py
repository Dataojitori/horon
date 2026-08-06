# System tag plugin: result
# Protected by SYSTEM_TAGS in db.py — cannot be deleted or renamed via normal operations.

DESCRIPTION = (
    "on_mutation（被任何涉及本概念的变更唤醒）：\n"
    "① 当本概念新增 result 标签时，检查其作为出边参与的所有 CHAIN，"
    "如果其前驱节点是 plan，则该 CHAIN 所在的组合概念必须是独占的（不可拥有其他变体），"
    "否则 reject（防止状态分叉）。\n"
    "audit：无，占位。"
)


def on_mutation(ctx):
    cid = ctx.this.concept_id
    
    if not ctx.changed:
        return
        
    added_tags = ctx.changed.get("tags", {}).get("added", [])
    if any(t["tag"] == "result" for t in added_tags):
        for uv in ctx.this.used_in_variations:
            if uv.type == "CHAIN":
                members = uv.members
                for my_idx, mem in enumerate(members):
                    if mem.concept_id == cid:
                        if my_idx > 0 and "plan" in members[my_idx - 1].tags:
                            holding_concept = ctx.get_concept(uv.concept_id)
                            if len(holding_concept.variations) > 1:
                                ctx.reject(
                                    f"排异反应：概念 '{holding_concept.name}' (id={holding_concept.concept_id}) "
                                    f"试图包含 [Plan -> Result] 闭环，但它已经拥有其他变体。\n"
                                    f"Plan -> Result 代表一个特定的因果假设，必须独占一个概念，"
                                    f"绝对不允许与其他表达式共享状态（否则会导致状态分叉）。"
                                )
                            if len(members) != 2:
                                ctx.reject(
                                    f"排异反应：不允许将明确的 [Plan -> Result] 验证闭环包裹进更长的序列中。"
                                    f"这会导致相同的逻辑在图谱中产生状态分叉。"
                                )


def audit_cluster(ctx):
    pass
