---
name: horon-plugin
description: 当你准备为 Horon 创建、修改或维护标签插件（Tag Plugin）、设计变动响应与指引钩子（on_mutation）、或者执行离线集群审计（audit_cluster）时，必须阅读此说明书。它包含了插件系统架构、制作规范与应用纪律。
---
# Horon 标签插件系统 (Tag Plugin System) 指导手册

本说明书是 Horon 标签插件的开发、使用与认知标准。它的核心目标是：**实现“测试驱动认知 (TDD for Thought)”，在干活之前先用插件为思维构造设定形状、边界与引导支架，防止图谱坍塌成滑坡的杂物山。**

---

# Part 1 · 核心哲学：测试驱动认知与思维架构 (TDD & Scaffolding)

## 1. 搬砖工 vs 架构师：先设形状，后盖小楼
- **平铺滑坡定律**：没有约束的自由图谱就像“卧室里堆满衣服的椅子”。如果任由节点平铺、不加限制，散落的概念最终只能堆成一座不断滑坡的杂物山。
- **思维的测试驱动设计 (TDD for Thought)**：你在准备操作一件事之前，脑子里其实是有结构的。不要等到堆了一堆散装节点再去清扫。**在干活前，先写 Tag 插件为你自己的思维构造设置形状与边界**——就像 TDD 里先写测试再写代码。
- **从杂物山到小楼**：插件把你的隐性认知架构硬化为物理代码。节点不再乱堆，而是顺着你预设的骨架自动搭建成稳固的小楼。

## 2. 插件的双重灵魂：活闸 (Gates) 与 导航支架 (Guides)
- **骨架约束与否决权 (`ctx.reject`)**：硬性规定该类概念的形态（如 `plan` 必须且只能是单向 `CHAIN`，`有出处` 必须附带源），违规形态拒绝落盘。
- **适时导航支架 (`ctx.info`)**：如同 `plan.py` 里的设计——当你在图里搭好第一块砖（确立了执行步骤），插件立刻在你的操作结果下递出下一步的结构手艺活（“下一步是连接 result，使用 `horon suppose A → B`”）。插件把思维导向直接内嵌到了节点本身。

---

# Part 2 · 插件制作三问：何时？干什么？怎么写？

## 1. 明确区分：什么时候造 Tag？什么时候造 Tag 插件 (Plugin)？

### 阶段 A · 什么时候制作/注册 Tag？（意图先行）
- **硬门槛（建 Tag 前必须过的关）**：你必须能回答——**"建了这个 Tag 之后，我能做什么现在做不了的事？"** 合法的回答只有以下几类：
  - **筛选送入流水线**："我需要从散装节点中筛出一批目标，送上后续阶段"（如 `求职目标公司`：筛选出具体公司 → 送上调研/排雷/応募流水线）。
  - **施加形状约束**："我需要强制这类节点只能长成某种拓扑"（如 `plan`：必须是 CHAIN，因为计划是一系列有序动作）。
  - **设置盖章门槛**："我需要在节点转 confirmed 时拦一道物理检查"（如 `有出处`：盖章前必须附可核实的源）。
  - **注入状态流转**："我需要让这类节点具备生命周期阶段"（如求职公司从未判读到候选适格到応募）。
  - ❌ **"把相似的东西放一起方便看"不是合法回答。** 那是归档，归档用 content 就够了，不需要 Tag。
- **反模式警告（归档癖）**：❌ "我看到好几个类似节点" → "造个桶装它们" → "给桶起个名字"——这条思维链产出的 Tag 永远只有分类功能、没有操作意图。名字会变成抽象名词拼接（如 `自有资产`、`硬性约束`、`排雷记录`），因为你心里没有动词，只有名词。
- **物理动作**：`horon create_concept <Tag名> --content "..."` + `horon create_tag <Tag名>`。
- **物理本质**：即使暂不写 Python 插件，Tag 的存在本身就承诺了一种操作意图。失忆者通过 `list_concepts --tag <Tag名>` 拉出全簇后，源概念正文必须能让他一眼看懂：这些东西被圈在一起是**为了做什么**，不只是它们**长得像**。

### 阶段 B · 什么时候才需要为 Tag 制作 Python 插件 (Plugin)？
- **核心判定**：当你需要把一种**“思维的形状”提炼成物理机制**——拓扑范式硬化、写时物理否决 (reject)、适时结构导航 (info)、集群审计 (audit)——而图里还没有现成插件承载这个形状时。
- **复用前置**：动手写 `.py` 之前先 `list_tags` 查现有插件；已有插件承载同一形状（如 `plan` 的单向链、`有出处` 的可核实性）则直接 `add tag` 复用，严禁重复造插件。
- **TDD for Thought (测试驱动认知)**：
  1. **允许提前设计骨架**：在进入全新领域或开展复杂推演前，你**完全可以提前设计 Tag 及其插件**——预设该领域的拓扑结构、红线死线与引导指引，为后续的思维搭建形状。
  2. **严禁“停止于写完”**：制作插件是推演的起点，绝不是交差的终点！绝对禁止在写完 `.py` 插件脚本后自我满足、不再展开具体的节点构建与 Tag 挂载（病理：把手段当成果的框架空转症）。
- **物理动作**：在 `backend/tag_plugins/<Tag名>.py` 编写 `on_mutation` 与 `audit_cluster`，并在随后展开的具体推演中将节点打上该 Tag 兑现骨架。

## 2. 插件可以用来干什么？ (What)
插件的核心用途在于将抽象的**认知范式**与**领域规则**降维硬化为系统的物理机制。具体涵盖五大核心应用场景：

1. **思维骨架与拓扑范式硬化 (Architecture Enforcement)**：
   - 将特定的推演范式（如 `plan` 只能是单向 `CHAIN` 闭包、单向依赖链、特定树状结构）强制硬化。违规形态拒绝落盘，强迫图谱顺着预设骨架生长为稳固的小楼，而不是坍塌成散装杂物山。
2. **适时结构导航与行动递加 (Scaffolding & Contextual Guidance)**：
   - 解决失忆与迷路问题。当你在图里搭好第一块砖，插件自动在你脚下递出下一个阶段的结构手艺活（如：“下一步使用 `horon suppose A → B` 连接预期结果”），把工作流的导航直接打包进节点本身。
3. **死线隔离与防污染警报 (Air-Gap & Anti-Pollution)**：
   - 在图谱中建立强效断层（如 `个人资产` 与 `职务` 节点结构性隔离，防范 IP 污染；`外部发布` 校验技能白名单，拦截编造数据；`有出处` 迫使关键事实附带可核实的 URL 或路径）。
4. **状态机与生命周期流转 (Lifecycle & State Machine)**：
   - 给静态概念注入生命周期与转换逻辑（如求职/提案管道的阶段晋级、从假设 `hypothesis` 到实锤 `confirmed` 的过审门槛），使节点具有状态流转能力。
5. **存量感知与全库架构巡检 (Cluster-Wide Architectural Audit)**：
   - 离线遍历整个标签簇，自动清点悬空节点、未结算假设、失修因果链与过时状态，输出系统的活账本（`horon audit`），代替人工肉眼扫图。

## 3. 如何制作插件？ (How - Development Spec)

### 3.0 铁律：Tag 命名契约与全宽对齐（意图可读 · 拒绝越权与名词拼凑）
- **主语与边界必须清晰（拒绝越权认领）**：
  - 如果一个 Tag 涉及所有权、能力或项目，它**必须包含明确的主语**（如 `Salem的...`，`Nocturne的...`）。
  - ❌ `自有资产`——谁的自有？这种无主语的词暴露了极度恶劣的边界感缺失：把 Salem 电脑上的私有资源（如游戏解包代码）理所当然地当作"自己"的资产去盘算变现。
  - ❌ `技能主张`——谁的主张？拿来干什么？看不出是"Salem 的某项能力"，也看不出具体意图。
- **允许使用项目级分类 Tag（作为工作区视图）**：
  - ✅ `Salem的求职project`——允许使用类似 `求职` 的分类词，前提是：主语清晰，且意图是作为**"快速打开工作区、铺桌子干活"**的视图聚合工具，而不是当散文垃圾桶。
- **名字必须透出操作意图**：
  - Tag 名不是在描述"里面装了什么"，而是让失忆者一读就知道**"拿里面的东西来干什么"**。好的 Tag 名背后藏着一个动词——即使名字本身是名词短语，读的人也能立刻反应出它的用途。
  - ✅ `求职目标公司`——读完就知道：这些公司是调研与投递的目标，要送上求职流水线。
  - ✅ `有出处`——读完就知道：这些节点必须附带可核实的源，用来保证结论可被证伪。
  - ❌ `硬性约束`——约束了谁的什么动作？两个形容词+名词的堆砌，没有主语也没有谓语。
  - ❌ `排雷记录`、`交付制品`、`求职管道阶段`——名词拼凑，看不出主语和操作意图，只是在给桶贴标签。
- **Tag 绝不是动作命令、规则说明书，更不是冰箱贴！** 严禁把"去核实白名单"、"个人资产不许挂职务边"这种控制动作或规则句子拿来当 Tag 名。
- **名字全宽即承诺**：Tag 的名字是给失忆后的自己与编译器看的全宽契约。**插件判据对受众节点的要求，必须恰好等于 Tag 名字的全部语义全宽。**
- **严禁拿抽象大词或半抽象缩写套壳（除非作为项目视图）**：
  - ❌ 错误示范：起名单个词 `求职`（大词套壳）或 `求职目标`（半缩写留退路）。若给 `求职策略` 打上 `求职` tag，插件把它当投递公司处理，就是典型的**封面与正文错位（包装欺诈）**。
  - ❌ 错误示范：起名 `核实技能白名单`（拿动作当 Tag）、`资产不准挂职务边`（拿规则句子当 Tag）。

### 3.1 文件位置与自述规范 (`DESCRIPTION`)
- 文件存放于 `backend/tag_plugins/<tag_name>.py`（文件名与 tag 名完全一致，如 `plan.py`、`有出处.py`、`求职目标公司.py`）。
- 顶部**必须**定义 `DESCRIPTION` 模块级字符串，用一段精准的自然语言同时概括 `on_mutation` 与 `audit_cluster` 的效果（`horon list_tags` 会多行缩进显示）：

```python
DESCRIPTION = (
    "on_mutation：变体转为 confirmed 时若正文缺乏 URL/文件路径则 reject。\n"
    "audit：列出已 confirmed 却缺乏出处的存量节点。"
)
```

### 3.2 必需入口函数
每个插件脚本**必须包含**以下两个入口函数（若某入口无逻辑，函数体内写 `pass`）。

> **注（自由定义）**：文件内可以根据需要自由定义辅助函数（如 `_has_source(text)`）、模块常量、内部 Class 等。引擎加载时仅校验这两个入口函数是否存在且可调用。

```python
def on_mutation(ctx):
    """当涉及本概念的变更发生时在事务提交前同步唤醒。
    ctx.this 代表概念当前拟态；ctx.changed 为本次 diff。
    阻断：ctx.reject("原因")
    提示：ctx.info("引导消息")
    """
    pass

def audit_cluster(ctx):
    """当运行 CLI `horon audit` 时被显式调用。
    遍历 ctx.cluster.concepts，批量检视存量。
    警告：ctx.warn("警告消息")
    """
    pass
```

### 3.3 `ctx` 接口与属性速查

#### `on_mutation(ctx)` 作用域：
- **`ctx.tag_name`** (`str`): tag 名称。
- **`ctx.this`** (`ConceptProxy`): 被检查或被卷入的概念代理（惰性、只读）。
  - `.concept_id` (`int`)
  - `.name` (`str`)
  - `.disclosures` (`list[str]`): 所有书腰文本列表
  - `.tags` (`list[str]`)
  - `.variations` (`list[VariationProxy]`): 本概念的变体列表。
  - `.used_in_variations` (`list[VariationProxy]`): 本概念被作为成员链入的所有外部变体。
  - **`VariationProxy` 字段**: `.concept_id`, `.short_code`, `.type` (`CHAIN`/`AND`/`OR`), `.status` (`hypothesis`/`confirmed`/`negated`), `.content`, `.valence`, `.members` (`list[ConceptProxy]`)
- **`ctx.changed`** (`dict`): 本次操作的 diff 字典。没改动的键不存在。
  - 常见结构：
    - `"variations"`: `{"added": [{"concept_id", "short_code", "type", "members"}], "removed": [...]}`
    - `"status"`: `{"concept_id", "short_code", "old", "new"}`
    - `"content"`: `{"concept_id", "short_code", "old", "new"}`
    - `"tags"`: `{"added": [{"concept_id", "tag"}], "removed": [...]}`
- **`ctx.reject(msg)`**: 阻断操作并触发事务回滚（消息直接反馈给用户）。
- **`ctx.info(msg)`**: 仅在整个事务成功提交后，将非阻断提示拼入操作结果中。

#### `audit_cluster(ctx)` 作用域：
- **`ctx.tag_name`** (`str`): tag 名称。
- **`ctx.cluster`** (`ClusterProxy`):
  - `.concepts` (`list[ConceptProxy]`): 属于该 tag 的所有概念。
  - `.count` (`int`): 概念总数。
- **`ctx.warn(msg)`**: 向审计报告中写入一条警告信息。

---

# Part 3 · 沙盒安全与语法禁区 (Sandbox Restrictions)

插件运行在严格的 AST 安全沙盒中（`tag_sandbox.py`），遵守以下铁律：

1. **受控的 `import` 模块白名单 (`_ALLOWED_IMPORTS`)**：
   - 插件中可以写标准的 `import re` 或 `from re import search`。
   - 试图导入非白名单模块（如 `import os` / `import sys` / `import subprocess`）会在加载期被 AST 校验拒绝并抛出 `TagPluginError`。
2. **内置白名单 (`_SAFE_BUILTINS`)**：
   - 可用内置函数：`len`, `str`, `int`, `bool`, `list`, `dict`, `set`, `tuple`, `range`, `enumerate`, `zip`, `any`, `all`, `min`, `max`, `sum`, `abs`, `float`, `sorted`, `True`, `False`, `None`。
   - 可用异常类：`Exception`, `ValueError`, `TypeError`, `KeyError`, `IndexError`, `AttributeError`, `RuntimeError`。
3. **函数调用黑名单 (`_BANNED_CALLS`)**：
   - 禁用 `eval`, `exec`, `open`, `compile`, `globals`, `locals`, `getattr`, `setattr`, `delattr`。
   - 注：`re.compile` 被黑名单按名拦截，请使用 `re.search(pattern, string)` 直调。
4. **禁止 Dunder (`__`) 属性访问**：
   - 任何以 `__` 起始的属性读写（如 `__closure__`, `__class__`, `__dict__`）均被 AST 校验拦截。

---

# Part 4 · 插件使用、调试与生命周期管理

## 1. 挂载与触发
- 为概念打上 tag：`python frontend/cli.py add 概念名 tag 标签名`
- 一旦挂载，针对该概念（或包含该概念为成员）的任何 `set`、`add`、`update`、`delete`、`suppose` 操作都会自动触发对应插件的 `on_mutation` 钩子。

## 2. CLI 命令操作
- **全库离线审计**：
  `python frontend/cli.py audit --all`
  一键扫过所有带插件的 tag 簇，输出汇总报告（例：`plan ⚠3 / result ✓ / 有出处 ⚠1`）。也可以针对特定 tag 审计：`python frontend/cli.py audit 有出处`。
- **查看已挂载 tag 及其插件说明**：
  `python frontend/cli.py list_tags`
  列出 tag 及其 `DESCRIPTION` 自述。

## 3. 热重载与改名联动
- **热重载 (mtime)**：修改 `backend/tag_plugins/<tag_name>.py` 代码后无需重启服务，引擎会在下一个事务前按 mtime 自动加载最新版本。
- **改名与删除**：对 tag 源概念执行 `set name` 或 `delete` 时，系统会自动对底层 `.py` 文件进行物理 rename 或 unlink，并清理内存插件缓存。

---

# Part 5 · 活用 Horon 插件的硬核纪律

1. **对齐死代码**：撰写 `DESCRIPTION` 与 `on_mutation` 逻辑时，必须对着死代码逻辑逐条推导，严禁凭借模糊的语义印象撰写。
2. **看准 `ctx.changed`**：写 `ctx.info()` 时必须比对 `ctx.changed` 是否包含了本次变更的相关字段（如 `variations.added`），避免被无害连带唤醒时向用户重复轰炸提示。
3. **拒绝套娃中间层**：拒绝为了“显得规整”而拆分多个没必要的辅助函数或中间抽象。1 个简洁的执行器 + 直白条件判断即可。
4. **Plugin-First**：遇到新的规则需求，优先考虑是否可以编译为 Tag 插件。当发现沙盒缺乏特定匹配能力时，优先使用笨而有效的方法（如纯 Python 的 substring 或 Jaccard token 重叠）实现，撞墙再向宿主申请扩展。
