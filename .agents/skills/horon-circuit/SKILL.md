---
name: horon-circuit
description: 当你需要把一条靠自觉记不住的做法做成自动触发的膝跳反射（传感器提醒或工具门禁），创建或修改 sensor、logic、guard，配置事件监听（sensor_hook、Jev 复筛、on_fire）、工具拦截（tool_guard）、抑制条件（inhibition），或把长期计划搭成电路时，阅读此说明书。
---

# Horon 电路与传感器制作说明书

本说明书介绍如何把环境事实、运行时事件、阶段依赖与工具调用门禁搭成 Horon 电路。日常概念读写、起名总则以及遇到现有电路时的诊断与应对，见 [horon-cli](../horon-cli/SKILL.md)。

---

# 1. 角色约束与命名要求

## 电路节点怎么起名

名字是给失忆后的自己看的，光看名字就要知道在这个情境下该怎么做，或者是什么具体条件成立了：

| 角色 | 怎么起名 | 好例 | 坏例（病因） |
| :--- | :--- | :--- | :--- |
| **传感器与逻辑** (`sensor/logic`) | 挂了传感器或电路的做法节点按「做法」起名（`[情境] + [我怎么做]`）；事件或事实条件用大白话写清**谁/什么发生了什么具体事**（分类出口固定为 `{Tag名}-exit`） | `工具输出中出现TimeoutError`<br>`用户本回合同意发出个人信息`<br>`陌生人按README能装上Horon`<br>`我说了记住但没写入` | `独立推理通道就绪`、`正则脱敏扫描已通过`、`算力自主`（抽象黑话或四字口号，不知所云）<br>`已扫描`（对象悬空） |
| **门禁** (`guard`) | `放行 + [具体操作/工具]` | `放行含个人信息的发帖`<br>`放行往boot里加节点` | `放行CLI`（动作与对象悬空）<br>`工具门禁`（把类型当成了指令） |

## 电路约束速查

| 项目 | plain | sensor | logic | guard |
| :--- | :--- | :--- | :--- | :--- |
| activation-rule | 无 | 无 | 必填 | 必填 |
| lifespan | 无 | 必填 | 无 | 无 |
| 正向上游输入 | 无 | 无 | 必须有 | 必须有 |
| 作为下游输入 | 不可 | 可以 | 可以 | 不可 |
| 作为抑制源 | 不可 | 可以 | 可以 | 不可 |
| 作为抑制目标 | 不可 | 不可 | 可以 | 可以 |
| sensor_hook | 不可 | turn/session，每节点一条 | 不可 | 不可 |
| 手动 set active | 不可 | 仅 permanent | 不可 | 不可 |
| tool_guard | 不可 | 不可 | 不可 | 每节点一条 |
| on_fire | 不可 | 可以 | 可以 | 可以 |

逻辑和守卫电位由依赖派生，不能手动设定。名称、内容和电路必须表达同一个状态；完成信号以实际结果为依据。

---

# 2. 输入与客观事实（`permanent` sensor）

sensor 的 `1` 表示定义的状态成立，`0` 表示尚未成立或已经失效。

节点的电位变化由其角色与生命周期通道驱动：
- **客观事实 (`permanent` sensor)**：跨会话保留并手动维护的事实，使用 `--lifespan permanent`。人工确认并保存依据后，通过 `set <事实状态名或ID> active 1` 维护，事实失效时设为 `0`；
- **运行时感知 (`turn` / `session` sensor)**：配置 `sensor_hook`，在系统捕获到匹配事件时由 Hook 自动点亮并随回合或会话重置；
- **逻辑与守卫 (`logic` / `guard`)**：电位完全由上游依赖条件自动推导派生。

```text
create_concept <事实状态名> --role sensor --lifespan permanent --content "该状态的判定标准与适用范围"
```

将 permanent 输入置为 1 前，正文必须已有可核实依据，例如成果版本与路径、检查命令及结果、验收记录。完成标准说明“怎样才算成立”，实际凭据说明“这一次为什么成立”。已有依据仍适用时无需重复追加。

实际完成并保存凭据后，更新电位并读取相关下游：

```text
update <事实状态名或ID> content --append-file <实际凭据文件>
set <事实状态名或ID> active 1
read_concept <依赖该事实的节点名或ID>
```

事实失效时，修正正文依据并用 `set <事实状态名或ID> active 0` 更新输入。更换成果版本后，需要重新判断旧凭据是否仍适用。

---

# 3. 让事件唤起相关记忆（`turn` / `session` sensor 与 `on_fire`）

sensor 可以配置 `sensor_hook` 监听运行时事件。事件命中时，系统把传感器置为 1；从 0 到 1 的跃迁会触发它的 `on_fire`。它可以单独用作事件提醒，也可以作为输入接进 logic 或 guard。logic 和 guard 通电时同样可以触发 `on_fire`，CHAIN 还处理终态迁移。

## sensor 的事件监听与生命周期

只有 `sensor` 可以挂载 Hook（`logic`、`guard` 与 `plain` 不支持），每个传感器一条。

### 事件类型

| 事件 (`event_type`) | 匹配的内容 | 触发时机 |
| :--- | :--- | :--- |
| `user_message` | 用户消息的文本 | 收到用户消息时，模型回复之前；适合对用户话里的线索作出响应 |
| `model_message` | 模型回复的文本 | 回复生成之后；通知在后续回合注入 |
| `tool_call` | 工具调用参数序列化后的 JSON | 工具执行之前 |
| `tool_result` | 工具返回的输出文本 | 工具返回之后 |

### 生命周期

绑定 Hook 的 sensor 必须使用 `turn` 或 `session`，由 Hook 自动置 1、按生命周期自动归零；`permanent` 只供手动维护事实，不能绑定 Hook。

- `turn`：回合结束时归零。回合结束指模型完成当前轮次的所有工具调用、交还话语权；单轮内的多次连续工具调用属于同一回合。适合响应瞬态事件，如单轮内的报错提醒、行为自检。
- `session`：本会话内保持为 1，换会话时归零。适合记录本会话已经发生的事，如“本会话已读取某规范”。

### 配置命令

```bash
python frontend/cli.py set <传感器名> sensor_hook <event_type> \
  --match-pattern "<正则>" \
  [--tool <工具名>] \
  [--jev "<是非题>"] [--jev-threshold 0.5] \
  [--adapter claude-code|codex|antigravity|opencode|dsh]
```

匹配分两步：先用正则筛；挂了 `--jev` 的传感器，正则命中后再由 Jev 按意思复筛。重新执行会覆盖原有 Hook。

- `--match-pattern`：必填 Python 正则，按 `re.search(..., re.IGNORECASE)` 匹配：全文任意位置命中即可，不区分大小写。要匹配某个工具的全部调用而不限制参数或结果，传入 `".*"`。
- `--tool`：只在 `tool_call` 与 `tool_result` 下有效，消息类事件不能指定。它是正则，与工具名整串匹配、区分大小写：`Bash` 只管 `Bash`，`Bash|bash` 同时管 Claude Code 和 opencode 的 shell。`--tool` 和参数正则按要管的那个宿主自己的工具名和参数名写（Claude Code 是 `Bash`、`"file_path"`；opencode 是 `bash`、`"filePath"`；dsh 是 `pwsh`、`horon` 的 `{"args": [...]}`），并加 `--adapter` 限定宿主。MCP 工具例外：各宿主统一叫 `mcp__<服务器>__<工具>`（如 `mcp__bluesky__send_post`），一条规则所有宿主通用，不加 `--adapter`。
- `--adapter`：可选，只在该宿主的会话里匹配。省略则所有宿主都匹配；宿主未知的会话（如终端手敲）只匹配省略了它的 Hook。重新绑定时省略 `--adapter` 会恢复为所有宿主。
- `--jev` 与 `--jev-threshold`：可选，正则命中后由 Jev（OpenRouter 上的判断模型，现在用的是 perplexity/pplx-decider-v1.2，模型名在 `backend/embedding.py` 的 `JEV_MODEL`；需 `OPENROUTER_API_KEY`）按意思复筛，用来减少正则的字面误报，比如引用、举例、否定里出现了同样的字。
  - 判定机制：Jev 判定为“是”的概率不低于 `--jev-threshold`（默认 0.5）才点火；调用失败（超时、无 Key 等）时降级按正则结果点火。
  - 输入边界：Jev 只看到当条事件文本，前面标明是谁说的，如 `用户对 AI 说：「……」`、`AI 对用户说：「……」`，看不到上下文历史。`tool_result` 在 Jev 那里是 `AI 调用工具 Bash 后，工具 Bash 返回：「……」`，只精确到工具名，Jev 不知道你敲的具体命令。
  - 出题规范：写一道单凭当条文本就能判断的是非题，写清这段文本是谁对谁说的、要判断的是它在表明什么；想排除的情形也写进问题里。如 `--jev "用户对 AI 说的这句话，是在表明用户自己同意 AI 把内容发出去吗？（转述别人的话、否定、提问都不算）"`。
  - 正则配合：仍需配置针对性初筛正则，避免使用 `".*"` 导致每条事件全量请求 Jev 产生延迟与费用。

### 误报与漏报的调优

平时遇到传感器误报或漏报时，先在传感器节点追加原始样本（见 `horon-cli` 第 3 节），不要凭单条样本立刻改正则或问题。积累了真实样本后，对照样本集一起调整 `--match-pattern` 或 `--jev` 问法。

## on_fire 动作

节点从 0 跃迁到 1 时执行 `on_fire` 配置的动作，目前可用的动作是 `notify`：把提醒文字推给 AI，开头标着来源节点。写法 `{"notify": "提醒文字"}`，再次设置会整体替换原配置。

只推提醒的传感器可以直接在真库建，建完用一条会命中的输入实测一次。

### 完整示例

工具输出出现 `TimeoutError` 时得到提醒：

```bash
python frontend/cli.py create_concept "工具输出中出现TimeoutError" --role sensor --lifespan turn --content "工具返回的文本中匹配到TimeoutError字样时通电。"
python frontend/cli.py set "工具输出中出现TimeoutError" sensor_hook tool_result --match-pattern "TimeoutError"
python frontend/cli.py set "工具输出中出现TimeoutError" on_fire '{"notify": "工具输出包含TimeoutError，请读取错误上下文判断原因。"}'
```

---

# 4. 组合条件、时序与长期计划（`logic`）

电路表达目标之间的关系。你可以把一个长期方向细化为阶段和小目标，用共同前置表达依赖，用可选路线表达分支。执行和反馈改变输入电位后，下游状态随拓扑重新计算，从局部变化看见全局影响。

## AND、OR 与 CHAIN

logic 与 guard 使用 `activation-rule` 连接已经存在的输入节点。下表中的 A、B 代表真实节点名（亦可使用节点 ID），实际命名仍需符合第 1 节的要求。

激活规则只支持单种运算符连接两个或多个节点，不支持括号 `()` 或复合表达式：

| 规则 | 表达的关系 | 局部行为 |
| :--- | :--- | :--- |
| `A & B` | 两项共同前置 | A=1、B=0 时为 0；两项都为 1 时为 1 |
| `A \| B` | 两条替代路线 | A=1、B=0 时为 1；A 失效后，若 B=1 仍保持 1 |
| `A → B` | 输入事件依次发生 | 收到 A 后等待 B；顺序推进到终点时成立（使用 Unicode `→`，不接受 `->`） |

- **单前置简写**：logic 与 guard 都可以只绑一个前置（`--activation-rule "A"`），系统归一化为单输入 `AND`，即 A 通电且未被抑制即成立。
- **单前置要配抑制**：`A UNLESS B` 表达「A 发生了而 B 没发生」，是监督与报警条件的常用形状。若单前置不带任何抑制源，它只是把 A 的电位原样转发一遍，这种情况直接用 A 本身，不要建节点。

```text
create_concept <组合状态名> --role logic --activation-rule "A & B" --content "为什么需要这两个条件，以及它们共同成立表示什么"
```

若需表达多层或组合条件，通过分层定义中间 logic 节点来实现：例如要表达 `A 之后满足 B 与 C`，先创建组合节点 `M`（规则为 `"B & C"`），再建立顶层规则 `"A → M"`。

输入变化后，下游随拓扑自动重新求值。新路线和依赖可以按实际认识调整。

## 长期状态与时序进度

permanent 输入跨会话保留，AND/OR 可据此重算长期项目状态。CHAIN 的推进进度属于当前会话，会话重置时清空；适合表达会话内先后发生的事件。长期阶段的完成事实保存在 permanent 输入及正文中。

---

# 5. 用守卫控制匹配的工具调用（`guard` 与 `inhibition`）

guard 将前置条件绑定到工具规则。匹配调用只有在守卫通电（PASS）时才放行；同一调用若匹配多个守卫，需要全部放行。未放行时，Hook Gateway 在 `PreToolUse` 阶段阻止执行。

## 前置条件与工具匹配

在实际需要限制某类调用、且相关条件节点已经存在时，创建守卫并绑定规则：

```text
create_concept <守卫名> --role guard --activation-rule "<前置状态名>" --content "这个前置条件为何决定该调用能否执行，以及适用的调用范围"
set <守卫名> tool_guard <实际工具名> [--args-pattern "参数匹配正则"] [--adapter claude-code|codex|antigravity|opencode|dsh]
```

工具名是正则，整串匹配、区分大小写（同上文 `--tool`）；`--args-pattern` 传入正则表达式，底层按 `re.search(..., re.IGNORECASE)` 匹配序列化后的参数 JSON（省略时匹配该工具全部调用）。前置状态为 0 时，匹配调用被阻止；前置成立且没有活跃抑制时放行。多个前置可按实际关系组合，守卫电位不能手动设置。

工具名和正则按要管的那个宿主自己的叫法写（各宿主的叫法见第 3 节「配置命令」的 `--tool`），一条守卫只管一个宿主的工具。`--adapter` 只在该宿主的会话里匹配；不加则所有宿主都匹配，宿主未知的会话（如终端手敲）只匹配不加的。重新绑定时省略 `--adapter` 会恢复为所有宿主。

配置后用 `read_concept` 查看守卫的 Tool Guard 和 Circuit，核对匹配范围及当前放行条件。

## 副本库验证（上线前必做）

**会拦截工具调用的守卫，先在副本库上试**：把要建的命令写成 batch 文件，在临时目录的仓库副本里（`.env` 的 `HORON_DB` 指向临时库）跑一遍，用会命中和不该命中的调用各试一次，再对真库执行。拦错了会把正在进行的会话卡死。

## 抑制条件（`inhibition`）

当某个状态成立应当否决放行（或否决某个 logic 节点成立）时，将已有 sensor 或 logic 作为抑制源：

```text
add <抑制源名> inhibition <目标名>
```

局部行为：前置=1、抑制源=0 时可放行；抑制源变成 1 后，即使前置仍为 1 也会阻止调用。抑制源恢复为 0 后，目标按前置重新求值，原有 CHAIN 进度保留。

抑制源只能是 sensor 或 logic，目标只能是 logic 或 guard，不能自抑制。抑制线可以解除：

```text
delete <抑制源名> inhibition <目标名>
```

修改工具参数与抑制源归零是不同的状态变化；应按传感器的 Hook 和生命周期确认它何时恢复，不能仅因参数已经修改就认定守卫已放行。

---

# 6. 电路命令参考

以下命令均接在 `python frontend/cli.py` 后。

| 操作 | 命令 |
| :--- | :--- |
| 创建电路节点 | `create_concept <名称> --content "正文" [--when-to-read "书腰"] --role sensor\|logic\|guard [--lifespan turn\|session\|permanent] [--activation-rule "..."] [--on-fire '...']` |
| 转换角色 | `set <名称或ID> role plain\|sensor\|logic\|guard [--lifespan turn\|session\|permanent] [--activation-rule "..."]` |
| 修改激活规则 | `set <名称或ID> activation-rule "规则"` |
| 修改生命周期 | `set <名称或ID> lifespan turn\|session\|permanent` |
| 手动拨动电位 | `set <名称或ID> active <0或1>` |
| 绑定事件感知 | `set <名称或ID> sensor_hook user_message\|model_message\|tool_call\|tool_result --match-pattern "正则" [--tool <工具名>] [--jev "是非题" [--jev-threshold 0.5]] [--adapter ...]` |
| 绑定工具守卫 | `set <名称或ID> tool_guard <工具名> [--args-pattern "正则"] [--adapter ...]` |
| 配置发火动作 | `set <名称或ID> on_fire '<动作JSON>'` |
| 添加/解除抑制 | `add <源名称或ID> inhibition <目标名称或ID>` / `delete <源名称或ID> inhibition <目标名称或ID>` |
| 清除规则并退回 plain | `delete <名称或ID> activation-rule` |
| 解绑感知或守卫 | `delete <名称或ID> sensor_hook` / `delete <名称或ID> tool_guard` |
| 逆向求解与诊断 | `compile --target <目标名称或ID> [--assume <传感器1> <传感器2> ...]` |
| 临时状态复位 | `reset` |

- `set role` 用于节点转职（转为 sensor 需附带 `--lifespan`，转为 logic/guard 需附带 `--activation-rule` 以满足底层约束）；若仅调整现有节点的规则或生命周期，直接使用独立的 `set activation-rule` 或 `set lifespan`。
- `set active` 仅允许对 `permanent` 传感器使用；`session` / `turn` 传感器由 Hook 自动感知，禁止手动拨动。
- `sensor_hook` 的 `--tool` 仅在 `tool_call` 与 `tool_result` 时可用，消息类事件严禁指定 `--tool`；`--match-pattern` 为必填。
- 删除电路节点时，若它仍被其他节点的 `activation-rule` 作为成员引用，系统会拒绝删除并列出相关节点；需先修改这些引用。删除组合节点只解除它与输入成员的关联，不删除那些成员。
- `reset`：仅用于脱离 IDE 的**离线终端调试**，手动清空测试残留的临时电位（turn/session 传感器及 CHAIN 进度），保留 permanent 客观事实。在 IDE 正常对话时，系统会在检测到新会话时由 Hook 自动完成重置，无需手动执行。
