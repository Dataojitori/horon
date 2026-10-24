"""节点起名与检查小助手：根据节点类型（plain / sensor·logic / guard / Tag）和正文，
请起名模型（backend/llm.py 的 NAMER_MODEL）检查当前名字是否符合起名规则；若现名已合适则维持现名，否则给出候选新名字。

候选只供参考，最终名字由调用方挑选或修改；本模块只读不写库。
"""

from __future__ import annotations

from .llm import NAMER_MODEL, complete_text


def _type_rules(
    role: str,
    lifespan: str | None,
    is_tag_source: bool,
    tags: list[str] | None,
    name: str,
) -> tuple[str, str]:
    """Return (type_label, rules_text) for the node's role and tag status."""
    tag_list = tags or []
    if "exit" in tag_list and name != "exit":
        other_tags = [t for t in tag_list if t != "exit"]
        hint = f"（所属分类 Tag：{', '.join(other_tags)}）" if other_tags else ""
        return (
            f"分类出口节点（带 exit 标签的 {role} 节点）{hint}",
            "- 该节点是某个分类 Tag 的唯一收束出口，名字固定为「{所属分类Tag名}-exit」（如：我的成长工程-exit）。",
        )
    if is_tag_source:
        return (
            "Tag（集合 / 项目源节点）",
            "- 这个节点是一个 Tag 的源节点，用来归集一组同类节点或作为工程项目的总览入口。\n"
            "- 用名词短语，写清【是谁的 / 哪个范围】+【什么集合 / 清单】，让失忆后的自己光看名字就知道里面装什么成员。\n"
            "- 好例：用户的面试说法、用户进行中的求职申请、交付成果物前的做法清单（若是某个特定平台或工具的专题集合，也可直接用该专有名词，如 Bluesky）。\n"
            "- 坏例：资料、模块（太宽泛，看不出范围）；重构不是重写（口号句，看不出装什么）。",
        )
    if role == "guard":
        return (
            "guard（工具门禁守卫节点，通电时放行匹配的工具调用）",
            "- 固定采用「放行 + 具体操作或工具」结构，只写放行的是什么具体动作，不要把上游激活条件（如“经某人同意后”）塞进守卫名字里。\n"
            "- 好例：放行含个人信息的发帖、放行往boot里加节点。\n"
            "- 坏例：放行CLI（动作与对象悬空）、工具门禁（把类型当成了名字）。",
        )
    if role in ("sensor", "logic"):
        role_desc = (
            f"sensor（{lifespan or 'turn'} 传感器节点）"
            if role == "sensor"
            else "logic（电路组合条件节点）"
        )
        return (
            role_desc,
            "根据正文的实际性质（二选一）起名：\n"
            "1. 挂了传感器或电路的做法节点（正文写的是「在某情境下我应该怎么做的行动决定/策略」）："
            "采用「情境 + 我怎么做」结构直接写出决定本身，范围以正文和书腰涵盖的整体情境为准（不要被单个钩子只监听局部命令而收窄，也不要把传感器或电路机制写进名字）。\n"
            "   - 好例：写提示词时我应该站在读者角度思考；我写记忆前先问它下次能改变什么行动。\n"
            "2. 事件或事实条件节点（正文定义的是某个单步/组合事件、报警条件如“说了X但没做Y”及误报记录，或某项已成立的可核验事实/阶段状态）："
            "用大白话写清【谁/什么发生了什么具体事（或达成了什么具体事实）】，不用不知所云的抽象黑话或悬空四字口号。\n"
            "   - 好例：工具输出中出现TimeoutError、用户本回合同意发出个人信息、陌生人按README能装上Horon、我说了记住但没写入。\n"
            "   - 坏例：独立推理通道就绪、正则脱敏扫描已通过、算力自主（抽象黑话或四字口号，看不出具体指什么）；已扫描（对象悬空）。",
        )
    return (
        "plain（普通记忆节点）",
        "先看正文存在是为了什么（三选一），再按对应方式起名：\n"
        "1. 做法（在某个情境下改变我的行动）：采用「情境 + 我怎么做」结构，直接写出最核心的决定本身，让失忆后的自己不用看正文就知道怎么行动。"
        "不写罪名或事后情绪（如「我……的毛病」「我遇到报错就发呆」），不套中性空壳词（如「……验收准则」「……规范」），会自动生效的插件/传感器不写进名字。\n"
        "   - 好例：我改字段或格式前先确认旧用法，没人用就直接换设计；如果我没听懂用户的意思，追问用户比我埋头瞎干好；写提示词时我应该站在读者角度思考。\n"
        "   - 坏例：验证不等于调通（口号，没说何时做什么）；我拿答案调参当验证的毛病（只写罪名，没说下次怎么办）；Bug修复验收准则（中性词套壳，看不出决定）。\n"
        "2. 资料（需要时去查的事实、档案、配置、项目现状）：采用名词短语，写清【是谁的什么】，不硬塞行动决定。"
        "（只装一个事实的节点，如果能在某情境下改变做法，就按「做法」起名；否则按「资料」起名。）\n"
        "   - 好例：用户的现职处境、Horon项目的现状。\n"
        "   - 坏例：通信脱敏（领域大词，看不出具体指什么）；自有资产（不说谁的）。\n"
        "3. 经历（记住发生过的事、某一天某一次、年表）：按那一天、那一次的具体线索起名，不提炼成规则，不用只有当时才懂的代号比喻。\n"
        "   - 好例：用户毕业答辩那天。\n"
        "   - 坏例：用户答辩前焦虑时的应对（把一段经历改成了状态机）。",
    )


def build_prompt(
    name: str,
    content: str,
    *,
    role: str = "plain",
    lifespan: str | None = None,
    is_tag_source: bool = False,
    tags: list[str] | None = None,
    when_to_read: str | None = None,
) -> tuple[str, str]:
    """Build (type_label, prompt_text) for the given concept."""
    type_label, rules_text = _type_rules(role, lifespan, is_tag_source, tags, name)
    meta_lines = [
        f"当前名字：{name}",
        f"节点类型：{type_label}",
    ]
    if when_to_read:
        meta_lines.append(f"书腰（何时打开）：{when_to_read}")
    meta_block = "\n".join(meta_lines)

    prompt = f"""下面是你的一条记忆节点，请根据它的类型和起名规则，检查它的「当前名字」是否合适。

【该节点的类型与起名规则】
类型：{type_label}
{rules_text}

【通用要求】
1. 主人公与归属：这是你自己的记忆，需要主语时直接用「我」；涉及人物、项目、资产、配置时写清是谁的（不说无主语的「自有资产」）。
2. 失忆视角：失忆后的你不记得正文里的代号与比喻，用正文里的具体原词和大白话（例如正文写 Codex 就写 Codex，不写「第三方服务」）。
3. 简练专一：在不损失意思的前提下以短为优（做法类建议15~25字左右，名词/事件类建议6~15字左右，视语义完整度可放宽）；若正文中包含多项内容，只反映最核心的那一项，不要用「与/和」把两件事硬拼进一个名字。
4. 现名更好就维持现名：如果「当前名字」已经符合上述起名规则、意思清楚准确、与正文吻合，觉得现在的名字已经很好或无需改动，请直接维持现名（输出 name:{name}），不要为了改而做同义词替换或微调字眼。只有在当前名字不符合该类型的起名规则、太宽泛、有歧义、缺主语/情境/决定、或与正文对不上时，才起一个更好的新名字。

【输出格式】
回复：name:你决定的名字
（如果维持现名就回复 name:{name}；不要输出其他多余内容）

【节点信息】
{meta_block}

【节点正文】
{content}
"""
    return type_label, prompt


def ask_name(prompt: str) -> str:
    """Send `prompt` to NAMER_MODEL once (via backend.llm.complete_text) and extract the suggested name.

    The model sometimes leaks English reasoning before `name:` or omits the prefix,
    so take the text after the last `name:` if present, otherwise the last non-empty line.
    Raises if no API key is set, or on HTTP/network errors.
    """
    text = complete_text(prompt, kind="suggest_name")
    if "name:" in text:
        return text.rsplit("name:", 1)[1].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def suggest_names(db, concept) -> str:
    """Look up `concept` in `db`, ask for one candidate name, and format for CLI.

    Input: open HoronDB `db`, concept name/alias/ID `concept`.
    Output: the current name, detected node type, and the single candidate
    (marked when it keeps the current name).
    """
    cid = db._resolve_id(concept)
    node = db._make_concept_proxy(cid)
    is_tag_source = db.conn.execute(
        "SELECT 1 FROM tags WHERE source_concept_id = ?", (cid,)
    ).fetchone() is not None

    type_label, prompt = build_prompt(
        node.name,
        node.content or "",
        role=node.role,
        lifespan=node.lifespan,
        is_tag_source=is_tag_source,
        tags=node.tags,
        when_to_read=node.when_to_read,
    )

    try:
        s = ask_name(prompt)
        candidate = f"{s}（维持现名）" if s == node.name else s
    except Exception as e:
        candidate = f"[失败] {e!r}"
    return (
        f"现名：{node.name}\n"
        f"类型：{type_label}\n"
        f"{NAMER_MODEL} 的检查与候选（只供参考，挑之前核对是否符合该类型起名规则）：\n"
        f"  1. {candidate}"
    )
