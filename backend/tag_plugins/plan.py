# System tag plugin: plan
# Protected by SYSTEM_TAGS in db.py — cannot be deleted or renamed via normal operations.

DESCRIPTION = (
    "on_mutation（被任何涉及本概念的变更唤醒，自行决定管不管）："
    "① 本概念只要存在 AND/OR 变体就 reject——plan 只能是 CHAIN（违规不是本次造成的也照拦）。"
    "② 本概念新建 CHAIN 且还没连到 result 时，提示去连 result。"
    "③ 本概念紧邻其后是一个 result、被一起链进某条 CHAIN 时被唤醒："
    "这条链正好是 plan→result 两节 → 提示假设待验证；"
    "plan→result 这对相邻被裹进更长的链（>2 节）→ reject"
    "（不许把验证闭环包进更长序列，会造成同一逻辑状态分叉）。\n"
    "audit：lint 出 [malformed] 没有指向 result 出边的 plan、"
    "[open] 出边仍是 hypothesis（未对现实结算）的 plan。"
)


def on_mutation(ctx):
    """Enforce plan constraints and provide guidance hints.

    Rules:
    1. A plan concept may only have CHAIN variations (reject AND/OR).
    2. First time a CHAIN is established on this plan: info about connecting to result.
    3. When this plan concept is linked as a member into a plan→result chain: info about
       verifying the hypothesis.
    """
    cid = ctx.this.concept_id

    # Rule 1: reject if any AND/OR variation exists on this concept
    if "plan" in ctx.this.tags:
        for v in ctx.this.variations:
            if v.concept_id == cid and v.type in ("AND", "OR"):
                ctx.reject(
                    f"Plan '{ctx.this.name}' contains a non-CHAIN expression "
                    f"({v.type}). plans can only contain CHAIN expressions.")

    # Only produce info hints if THIS mutation actually changed something relevant
    if not ctx.changed:
        return

    # Rule 2: first CHAIN hint (only when variations were added to THIS concept)
    added_vars = ctx.changed.get("variations", {}).get("added", [])
    my_new_chains = [
        v for v in added_vars
        if v["concept_id"] == cid and v["type"] == "CHAIN"
    ]
    if my_new_chains and "plan" in ctx.this.tags:
        has_result_link = False
        for uv in ctx.this.used_in_variations:
            if uv.type == "CHAIN" and len(uv.members) == 2 and uv.members[0].concept_id == cid:
                if "result" in uv.members[1].tags:
                    has_result_link = True
                    break
                    
        if not has_result_link:
            ctx.info(
                f"\n\n[ACTION REQUIRED]\n"
                f"你已经为计划确立了执行步骤。下一步是将它连接到预期结果：\n"
                f"`horon suppose \"{ctx.this.name} → 预期结果概念\"`\n"
                f"（预期结果概念应带有 result 标签。如果还没有，先用 `horon init_result <name> --content \"...\"` 创建。）"
            )

    # Rule 3: plan→result link detection
    # When a new CHAIN variation has this plan concept as a member,
    # check if it forms a plan→result pattern
    if "plan" not in ctx.this.tags:
        return

    for v in added_vars:
        if v["concept_id"] == cid:
            continue
        if cid not in v.get("members", []):
            continue
        if v["type"] != "CHAIN":
            continue
        members = v["members"]
        for my_idx, mid in enumerate(members):
            if mid != cid:
                continue
            if my_idx + 1 >= len(members):
                continue
            for uv in ctx.this.used_in_variations:
                if uv.concept_id == v["concept_id"] and uv.type == "CHAIN":
                    if my_idx + 1 < len(uv.members):
                        next_concept = uv.members[my_idx + 1]
                        if "result" in next_concept.tags:
                            if len(members) == 2:
                                ctx.info(
                                    f"\n\n[ACTION REQUIRED]\n"
                                    f"你刚刚建立了一个 Plan → Result 的预期链路。\n"
                                    f"注意：此连接当前处于 hypothesis (假设) 状态。\n"
                                    f"验证有了结果后，把验证过程和凭据 update 进该概念（id={v['concept_id']}）的 content。"
                                )
                            else:
                                ctx.reject(
                                    f"排异反应：不允许将明确的 [Plan -> Result] 验证闭环包裹进更长的序列中。"
                                    f"这会导致相同的逻辑在图谱中产生状态分叉。"
                                )
                    break


def audit_cluster(ctx):
    """Plan hygiene check: two lints.

    [malformed] plan has no outbound CHAIN edges (no expectations defined).
    [open] plan's outbound edge is still hypothesis (not yet settled).
    """
    for concept in ctx.cluster.concepts:
        cid = concept.concept_id
        outbound_result_edges = []
        hypothesis_edges = []

        for uv in concept.used_in_variations:
            if uv.type != "CHAIN":
                continue
            members = uv.members
            
            is_outbound_expectation = False
            for idx, mem in enumerate(members):
                if mem.concept_id == cid:
                    if idx + 1 < len(members) and "result" in members[idx + 1].tags:
                        is_outbound_expectation = True
                        break
                        
            if is_outbound_expectation:
                outbound_result_edges.append(uv)
                if uv.status is None or uv.status == "hypothesis":
                    # Get the expression for reporting
                    member_names = [m.name for m in members]
                    expr = " → ".join(member_names)
                    hypothesis_edges.append((uv, expr))

        if not outbound_result_edges:
            ctx.warn(
                f"[malformed] plan '{concept.name}' "
                f"(id={cid}) has no outbound expectation. "
                f"A plan must chain into the result you expect from it.")
            continue

        for uv, expr in hypothesis_edges:
            ctx.warn(
                f"[open] plan '{concept.name}' (id={cid}): "
                f"expectation '{expr}' "
                f"(see \"{uv.concept_name}\") is still a hypothesis. "
                f"Settle it against reality: confirm or negate.")
