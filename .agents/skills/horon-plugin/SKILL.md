---
name: horon-plugin
description: 当你准备为 Horon 创建、修改或维护标签插件（Tag Plugin）、设计变动响应与指引钩子（on_mutation）、或者执行离线集群审计（audit_cluster）时，必须阅读此说明书。它包含了插件系统架构、制作规范与应用纪律。
---
# Horon 标签插件系统 (Tag Plugin System) 指导手册 (Harness v3)

本说明书是 Horon 标签插件的开发、使用与认知标准。它的核心目标是：**实现“测试驱动认知 (TDD for Thought)”，在干活之前先用插件为思维构造设定形状、边界与引导支架，防止图谱坍塌成滑坡的杂物山。**

---

# Part 1 · 核心哲学：测试驱动认知与思维架构 (TDD & Scaffolding)

## 1. 搬砖工 vs 架构师：先设形状，后盖小楼
- **平铺滑坡定律**：没有约束的自由图谱就像“卧室里堆满衣服的椅子”。如果任由节点平铺、不加限制，散落的概念最终只能堆成一座不断滑坡的杂物山。
- **思维的测试驱动设计 (TDD for Thought)**：你在准备操作一件事之前，脑子里其实是有结构的。不要等到堆了一堆散装节点再去清扫。**在干活前，先写 Tag 插件为你自己的思维构造设置形状与边界**——就像 TDD 里先写测试再写代码。
- **从杂物山到小楼**：插件把你的隐性认知架构硬化为物理代码。节点不再乱堆，而是顺着你预设的骨架自动搭建成稳固的小楼。

## 2. 插件的双重灵魂：活闸 (Gates) 与 导航支架 (Guides)
- **骨架约束与写时否决 (`ctx.reject`)**：硬性规定该类概念的形态（如 `action` 必须包含 `<action>` 闭包，`exit` 必须命名为 `{group}-exit`），违规形态拒绝落盘并触发事务回滚。
- **适时导航支架 (`ctx.info`)**：当你在图里搭好第一块砖，插件立刻在你的操作结果下递出下一步的结构手艺活（“下一步是创建守卫节点并 `set tool_guard` 绑定工具”），把工作流的导航直接内嵌到节点本身。

---

# Part 2 · 插件制作三问：何时？干什么？怎么写？

## 1. 明确区分：什么时候造 Tag？什么时候造 Tag 插件 (Plugin)？

### 阶段 A · 什么时候制作/注册 Tag？（意图先行）
- **硬门槛（建 Tag 前必须过的关）**：你必须能回答——**"建了这个 Tag 之后，我能做什么现在做不了的事？"** 合法的回答只有以下几类：
  - **筛选送入流水线**："我需要从散装节点中筛出一批目标，送上后续阶段"（如 `待精读论文`：从读过摘要的论文里筛出一批 → 送上精读/复现流水线）。
  - **施加形状与闭包约束**："我需要强制这类节点正文必须持有某种验证闭包"（如 `state` / `action`：必须包含 `<state>` / `<action>` 闭包块）。
  - **设置出口收束点**："我需要约束整个族群必须拥有单一目标出口"（如 `exit`：强制 `{group}-exit` 命名与收束）。
  - ❌ **"把相似的东西放一起方便看"不是合法回答。** 那是归档，归档用 content 就够了，不需要 Tag。
- **物理动作**：`python frontend/cli.py create_concept <Tag名> --content "..."` + `python frontend/cli.py create_tag <Tag名>`。

### 阶段 B · 什么时候才需要为 Tag 制作 Python 插件 (Plugin)？
- **核心判定**：当你需要把一种**“思维的形状”提炼成物理机制**——拓扑范式硬化、写时物理否决 (`reject`)、适时结构导航 (`info`)、集群审计 (`audit`)——而图里还没有现成插件承载这个形状时。
- **复用前置**：动手写 `.py` 之前先 `list_tags` 查现有插件；已有插件承载同一形状则直接 `add tag` 复用，严禁重复造插件。
- **物理动作**：在 `backend/tag_plugins/<Tag名>.py` 编写 `on_mutation` 与 `audit_cluster`。
- **写完插件不是终点**：插件只是给后续推演搭的骨架。写完 `.py` 后要接着建具体节点、挂上 Tag 兑现它；只写框架不填节点，是把手段当成果的空转。

---

## 2. 如何制作插件？ (Development Spec)

### 2.0 铁律：Tag 命名契约与全宽对齐
- **主语与边界必须清晰**：若涉及所有权或私有项目，必须包含明确主语（如 `alice的技术栈`，而不是无主语的 `自有资产`——谁的？）。
- **名字必须透出操作意图**：Tag 名让失忆者一读就知道**拿里面的东西来干什么**（如 `待精读论文`、`需要出处`）。
- **名字全宽即承诺**：插件判据对受众节点的要求，必须恰好等于 Tag 名字的全部语义全宽。

### 2.1 文件位置与自述规范 (`DESCRIPTION`)
- 文件存放于 `backend/tag_plugins/<tag_name>.py`（文件名与 tag 名完全一致）。
- 顶部**必须**定义 `DESCRIPTION` 模块级字符串（非空），在一处同时概括 `on_mutation` 与 `audit_cluster` 的效果；缺失或为空时插件加载失败（`TagPluginError`）。`list_tags` 会显示它：

```python
DESCRIPTION = (
    "on_mutation：概念新增或修改 content 时，若正文缺乏可核实的 URL/文件路径则 reject。\n"
    "audit：检查所有该标签概念，列出正文缺乏有效出处的存量节点。"
)
```

### 2.2 必需入口函数
每个插件脚本**必须包含**以下两个入口函数（无逻辑写 `pass`）。文件内可以自由定义辅助函数与模块常量，引擎只校验这两个入口是否存在且可调用：

```python
def on_mutation(ctx):
    """当涉及本概念的变更发生时在事务提交前同步唤醒。
    ctx.this 代表概念当前拟态；ctx.changed 为本次 diff。
    阻断：ctx.reject("原因")
    提示：ctx.info("引导消息")
    """
    pass

def audit_cluster(ctx):
    """当运行 CLI `audit <tag>` 或 `audit --all` 时被显式调用。
    遍历 ctx.cluster.concepts，批量检视存量。
    警告：ctx.warn("警告消息")
    """
    pass
```

---

### 2.3 `ctx` 接口与属性速查 (Harness v3 规范)

#### `ConceptProxy` 只读属性：
- **`.concept_id`** (`int`): 概念主键 ID。
- **`.name`** (`str`): 概念显示名称。
- **`.content`** (`str | None`): 概念正文文本。
- **`.role`** (`str`): 角色（`'plain'`, `'sensor'`, `'logic'`, `'guard'`）。
- **`.is_active`** (`int`): 当前物理电位（`0` 或 `1`）。
- **`.lifespan`** (`str | None`): 传感器生命周期（`'turn'`, `'session'`, `'permanent'` 或 `None`）。
- **`.activation_type`** (`str | None`): 激活规则类型（`'AND'`, `'OR'`, `'CHAIN'` 或 `None`）。
- **`.on_fire`** (`str | None`): 发火动作 JSON 配置文本。
- **`.tags`** (`list[str]`): 当前概念持有的标签列表。
- **`.disclosure`** (`str | None`): 一句话书腰（触发场景）。
- **`.inputs`** (`list[ConceptProxy]`): 上游输入引脚（当前概念在 `activation-rule` 中依赖的前置成员概念列表；CHAIN 的顺序与回访会保留，可能有重复）。
- **`.downstream`** (`list[ConceptProxy]`): 下游承接节点（将当前概念作为输入引脚引用的所有下游逻辑中继与守卫列表，已去重）。
- 查询不存在的概念时，标量字段返回默认值（`""` / `"plain"` / `0` / `None`），列表字段返回 `[]`。

#### 触发范围：`on_mutation` 什么时候被唤醒？
对某概念持有的每个带插件的 tag，以下情况都会唤醒该插件的 `on_mutation`（事务提交前同步执行）：
- 该概念本身被修改：content、名称/别名、书腰、tag、role、lifespan、activation-rule；
- 该概念作为成员被链入或移出某个父概念的 activation-rule；
- 该概念被删除（在真正删除前唤醒，此时仍可读取它的字段），或它的父概念被删除。

所以插件会经常被**连带唤醒**。写 `ctx.info()` 前先检查 `ctx.changed` 里是否有你关心的键，否则每次无关变更都会向用户重复轰炸提示（参考 `state.py`：只在 `tags.added` 含 `state`，或 `content` 变更且已持有该 tag 时才检查）。

#### `on_mutation(ctx)` 作用域：
- **`ctx.tag_name`** (`str`): 当前触发的 tag 名称。
- **`ctx.this`** (`ConceptProxy`): 被操作或被卷入的概念代理。
- **`ctx.changed`** (`dict`): 本次操作的 diff 字典。没改动的键不存在。
  - 结构示例：
    - `"content"`: `{"concept_id": 1, "old": "...", "new": "..."}`
    - `"tags"`: `{"added": [{"concept_id": 1, "tag": "..."}], "removed": [...]}`
    - `"activation-rule"`: `{"concept_id": 1, "old": "...", "new": "A & B"}`
    - `"role"`: `{"concept_id": 1, "old": "plain", "new": "logic"}`
    - `"lifespan"`: `{"concept_id": 1, "old": "session", "new": "permanent"}`
    - `"disclosure"`: `{"concept_id": 1, "old": "...", "new": "..."}`（删除书腰时 `new` 为 `None`）
    - `"names"`: `{"added": [{"concept_id": 1, "name": "..."}], "removed": [...]}`（别名增删）
    - `"concepts"`: `{"removed": [{"concept_id": 1, "name": "...", "members": [2, 3]}]}`（概念被删除；被删概念本身与其成员都会收到这份 diff）
- **`ctx.reject(msg)`**: 阻断当前操作并触发事务回滚（消息直接反馈给用户）。
- **`ctx.info(msg)`**: 仅在事务成功提交后，向用户输出结构化引导提示。
- **`ctx.get_concept(concept_id)`**: 按 ID 取任意概念的 `ConceptProxy`，用于跨节点检查。

#### `audit_cluster(ctx)` 作用域：
- **`ctx.tag_name`** (`str`): tag 名称。
- **`ctx.cluster`** (`ClusterProxy`):
  - `.concepts` (`list[ConceptProxy]`): 属于该 tag 的所有概念代理。
  - `.count` (`int`): 概念总数。
- **`ctx.warn(msg)`**: 向审计报告中写入一条警告信息。
- **`ctx.get_concept(concept_id)`**: 同上。

---

# Part 3 · 沙盒安全与语法禁区 (Sandbox Restrictions)

插件运行在严格的 AST 安全沙盒中（`tag_sandbox.py`），遵守以下铁律：

1. **受控的 `import` 白名单**：仅允许 `import re` 或 `from re import search`。禁止导入 `os`, `sys`, `subprocess` 等。
2. **内置白名单**：
   - 函数：`len`, `isinstance`, `str`, `int`, `bool`, `list`, `dict`, `set`, `tuple`, `range`, `enumerate`, `zip`, `any`, `all`, `min`, `max`, `sum`, `abs`, `round`, `float`, `sorted`, `True`, `False`, `None`。
   - 异常类（可 `try/except`）：`Exception`, `ValueError`, `TypeError`, `KeyError`, `IndexError`, `AttributeError`, `RuntimeError`, `ZeroDivisionError`, `StopIteration`。
3. **函数调用黑名单**：禁用 `eval`, `exec`, `open`, `compile`, `globals`, `locals`, `getattr`, `setattr`, `delattr`。
   - 注：`re.compile` 会被拦截，请统一使用 `re.search(pattern, string)` 直调。
4. **禁止 Dunder 属性访问**：任何以 `__` 起始的属性访问（如 `__dict__`, `__class__`）均被拦截。

---

# Part 4 · 插件生命周期与调试

1. **挂载标签**：`python frontend/cli.py add <概念名> tag <标签名>`
2. **离线集群审计**：`python frontend/cli.py audit <标签名>` 或 `python frontend/cli.py audit --all`
3. **热重载 (mtime)**：修改 `backend/tag_plugins/<tag_name>.py` 后无需重启服务，引擎会在下一个事务开始前自动重新加载。
4. **改名与删除联动**：对 tag 源概念执行 `set name` 或 `delete` 时，系统会自动对底层 `.py` 文件进行物理 rename 或 unlink。
