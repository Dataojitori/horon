# Horon Harness 重构计划

## 1. 我们要造什么

Horon 不再以“在概念之间建立并证明静态哲学关系”为中心，而是成为一副接在 AI 外面的**神经—效应 Harness 系统（Executable Cognitive Harness）**：

```text
外界事实 / 手动开关 / 被动感知 (Sensor Hooks)
                    ↓
        传感器与叶子节点 (is_active = 1, role = 'sensor')
                    ↓
     逻辑神经元 (AND / OR / CHAIN, compose_members, role = 'logic')
                    ↓
    放行守卫与 On-Fire 副作用 (Guards & On-Fire Actions, role = 'guard')
                    ↓
     工具物理拦截 / 放行 / 状态抑制 (Tool Guards)
                    ↑
   当前阻塞差集 ──→ suggest ──→ 下一步注意力
```

它的实际价值只有两项：

1. **注意力引导**：让 AI 在正确的时候看到正确的概念（由现有 `suggest` 和发火后的 `set_focus` 负责）。
2. **物理边界拦截**：在 AI 越过手续或前置未满足时，**从工具网关物理拦住它**（基于规则匹配的动态门禁），而不是写一条希望它自觉遵守的提示词。

> [!NOTE]
> **重构进度与实装状态最新汇总（截至 2026-10-02 全量核对完毕）**：
> - **已完工核心（Step 1 ～ Step 8 全部交付）**：
>   - **Step 1（Schema / 迁移 / 数据模型）**：落地扁平概念、四类角色、互斥约束，以及 `005_harness_v3.sql`、`006_current_session.sql`、`007_pending_notifications.sql`、`008_add_snapshots.sql`。
>   - **Step 2（核心求值引擎）**：`backend/evaluator.py` 完整实现 Kahn DAG 拓扑单趟求值、AND/OR/CHAIN NFA 时序波前、抑制压制、上升沿 `on_fire` 与生命周期复位。
>   - **Step 3（网关 + 逆推求解器 + §6 验收）**：逆推差集求解器实装于 `backend/_db_compile.py`，CLI `compile --target [--assume]` 导通；§6 五项检查在 2026-09-23 全量通过。
>   - **Step 4（CLI Harness 适配）**：`frontend/cli.py` 全面落地专属电路分区视图、电位展示、Harness v3 管理命令族。
>   - **Step 5（出入边与旧关系模型拔除）**：彻底清除 `inbound`/`outbound`/`DirectedRelation` 残留。
>   - **Step 6（Web 可视化与 API 只读隔离）**：`server.py`、`types.ts`、`InspectorSidebar`、`DissectionView` 完整适配 Harness v3 数据结构与注意力跳转。
>   - **Step 7（插件系统清理与 Agent 技能手册）**：`backend/_db_plugins.py` 适配扁平代理，清理旧变体依赖，技能手册全量更新。
>   - **Step 8（物理 Hook 网关与 IDE 生命周期闭环）**：核心动作收敛在 `backend/harness/core.py`，入口分发在 `backend/hook_gateway.py`，支持 antigravity / claude-code / codex 三宿主全生命周期拦截与感知。
> - **后续清理与数据补全（已全部结案）**：
>   1. **[✓ 已完成] 删除旧 `backend/compiler.py`**：已于 2026-09-23（commit `b53c2657`）物理删除。
>   2. **[✓ 已完成] 真库角色契约与写入校验**：已于 2026-09-26（commit `f366355c`）在 `backend/db.py` 中增加 `_check_circuit_members` 与 `_check_role_change_keeps_members_valid` 强制校验；真库中所有前置成员均已转换为合规的 `sensor`（如「独立推理通道就绪」等），数据库完整性检查（`audit_db_integrity`）报告 0 违规。
>   3. **[注记] 全景星图（GalaxyView 2D Canvas）**：节点数据（role 与 is_active）已在 API `/api/graph` 全量下发；星图前端按设计保留为文本体积膨胀热度图（byte_size 渐变与超重告警），角色与通电状态集中由侧边栏检查器与解剖视图（DissectionView）承载。
> - **实装与原计划细微差异（以已实装为准）**：
>   - **会话持久化**：通过 `current_session` 表（Migration 006）写入 SQLite，脱机兜底为 `devonly`。
>   - **通知缓冲队列**：新增 `pending_notifications` 表（Migration 007）解耦工具阶段通知与生成前置注入；统一通过 `wrap_harness_message` 打上 `【Horon Harness】` 信头，防止模型回音反向激发传感器。
>   - **发火动作白名单与结构化**：白名单收敛为 `notify`、`set_focus`、`add_todo`，支持纯文本自动包装，返回强化为 `FiredAction(concept, concept_id, action)`。
>   - **网关物理形态**：核心动作收敛在 `backend/harness/core.py`，`backend/hook_gateway.py` 是多宿主入口（antigravity / claude-code / codex 适配器）。
>   - **逆推求解器位置**：求解器实装在 `backend/_db_compile.py`；在 SAVEPOINT 里调用真实 `GraphEvaluator` 求值后回滚，保证诊断与运行时逐位一致。

---

## 2. 核心契约与数据结构

### 2.1 全角色与拓扑连接约束矩阵（Master Contract Matrix）

所有规则与连接边界集中收敛于此表，杜绝规则散落：

| 维度 / 约束 | 1. 普通砖块 (`plain`) | 2. 传感器 (`sensor`) | 3. 逻辑中继 (`logic`) | 4. 放行守卫 (`guard`) |
| :--- | :--- | :--- | :--- | :--- |
| **真实定位** | 静态知识、实体与手册 | 感觉传入神经 / 外部事实源 | 中间组合逻辑与时序锁存 | 工具出口放行阀门 / 效应器 |
| **电位性质** | 恒为 0（无电位） | 原始原子电位 (0 或 1) | 物化派生电位 (0 或 1) | 物化派生电位 (0 或 1) |
| **电位来源** | 无（不参与电路） | `permanent` 仅人工 `set`；`session`/`turn` 仅 `sensor_hooks` | Kahn 拓扑单趟求值缓存 | Kahn 拓扑单趟求值缓存 |
| **`activation_type`** | **恒为 NULL** | **恒为 NULL** (入度恒为0) | **必填** (`AND`/`OR`/`CHAIN`) | **必填** (`AND`/`OR`/`CHAIN`) |
| **`lifespan`** | **恒为 NULL** | **必填** (`turn`/`session`/`permanent`) | **恒为 NULL** | **恒为 NULL** |
| **`sensor_hooks` 绑定** | ❌ 严禁 (非电路节点) | ⚠️ **仅限 `turn`/`session`** (`permanent` 严禁) | ❌ 严禁 (非输入端) | ❌ 严禁 (非输入端) |
| **手动 `set active` 权限** | ❌ 严禁 (电位恒为 0) | ⚠️ **仅限 `permanent`** (`session`/`turn` 严禁) | ❌ 严禁 (电位为拓扑计算派生) | ❌ 严禁 (电位为拓扑计算派生) |
| **正向入边 (`parent`)** | ❌ 严禁 (无上游) | ❌ 严禁 (入度恒为 0) | ✅ 必须有 (构成激活规则依赖) | ✅ 必须有 (构成激活规则依赖) |
| **正向出边 (`member`)** | ❌ 严禁 | ✅ 允许 (作为下游的输入) | ✅ 允许 (作为下游的输入) | ❌ 严禁 (出口端不再充当下游输入) |
| **抑制目标 (`target`)** | ❌ 严禁 (无电位可压制) | ❌ 绝对严禁 (事实不可篡改) | ✅ **允许** (阻断中间级联) | ✅ **允许** (出口一票否决) |
| **抑制源 (`inhibitor`)**| ❌ 严禁 (电位恒为 0) | ✅ **允许** (事实直接触发抑制) | ✅ **允许** (复合逻辑触发抑制) | ❌ 严禁 (避免输出端回环) |
| **`on_fire` 副作用** | ❌ 无效 | ✅ 上升沿触发 (0→1) | ✅ 上升沿 / 终态迁移触发 | ✅ 上升沿触发 (0→1) |

---

### 2.2 数据库 Schema 全景（7 张核心表）

```sql
-- 1. 概念主表（彻底废除 variations，1 Concept = 1 扁平实体）
CREATE TABLE IF NOT EXISTS concepts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    content         TEXT,                           -- 描述、事实、证据笔记
    disclosure      TEXT,                           -- 一句话触发场景书腰 (1:1，直接挂在 concepts 表)
    role            TEXT    NOT NULL DEFAULT 'plain'-- 'plain'(砖块), 'sensor'(传感器), 'logic'(逻辑中继), 'guard'(放行守卫)
                            CHECK(role IN ('plain', 'sensor', 'logic', 'guard')),
    
    -- 电位状态（直接挂在概念上，单点读写；反映当前节点的客观物理电位：0=灭/断电, 1=亮/通电）
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),     
    
    -- 传感器保鲜期 (仅 role = 'sensor' 有效；非传感器恒为 NULL)
    lifespan        TEXT    CHECK(lifespan IS NULL OR lifespan IN ('turn', 'session', 'permanent')),
    
    -- 激活规则类型 (仅 role = 'logic' 或持有规则的 'guard' 有效；sensor 与 plain 恒为 NULL，绝无上游计算依赖)
    activation_type TEXT    CHECK(activation_type IS NULL OR activation_type IN ('CHAIN', 'AND', 'OR')),
    
    -- 发火动作配置 (JSON 文本或文本白名单，支持 notify, set_focus, add_todo)
    -- 例：{"set_focus": "代码实装规范"} 或 "直接告警通知"
    on_fire         TEXT,                           
    
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    -- 严格的角色互斥与字段完整性约束（plain 节点电位恒为 0）
    CHECK (
        (role = 'plain'  AND activation_type IS NULL     AND lifespan IS NULL     AND is_active = 0) OR
        (role = 'sensor' AND activation_type IS NULL     AND lifespan IS NOT NULL) OR
        (role = 'logic'  AND activation_type IS NOT NULL AND lifespan IS NULL) OR
        (role = 'guard'  AND activation_type IS NOT NULL AND lifespan IS NULL)
    )
);

-- 2. 组合逻辑拓扑表（正向依赖边：member -> parent）
CREATE TABLE IF NOT EXISTS compose_members (
    parent_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    member_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    order_index       INTEGER NOT NULL DEFAULT 1,   -- CHAIN 按序排列 (1, 2, 3...)；AND/OR 默认 1, 2, 3...
    PRIMARY KEY (parent_concept_id, order_index)    -- 保证同一父概念下每个步骤序号唯一；允许 CHAIN 出现时序回访（如 A → B → A）
);
CREATE INDEX IF NOT EXISTS idx_cm_member ON compose_members(member_concept_id);

-- 3. 输入端：被动感知钩子表（感觉传入神经）
CREATE TABLE IF NOT EXISTS sensor_hooks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_concept_id INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE, -- 1:1 物理锁死
    event_type        TEXT    NOT NULL              -- 'user_message', 'model_message', 'tool_call', 'tool_result'
                      CHECK(event_type IN ('user_message', 'model_message', 'tool_call', 'tool_result')),
    tool              TEXT,                         -- 工具名 (仅 tool_call / tool_result 时有效；消息类为 NULL)
    match_pattern     TEXT    NOT NULL,             -- 全文正则：匹配消息文本 / 工具返回值 / 调用参数序列化文本
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sh_lookup ON sensor_hooks(event_type, tool);
-- 表达式唯一索引防 NULL 穿透（SQLite 中 NULL=NULL 为假，使用 IFNULL 确保规则唯一绑定）
CREATE UNIQUE INDEX IF NOT EXISTS uidx_sh_event ON sensor_hooks(event_type, IFNULL(tool, ''), match_pattern);

-- 4. 输出端：工具放行守卫规则表（效应器 / 工具拦截与放行网关）
CREATE TABLE IF NOT EXISTS tool_guards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guard_concept_id  INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE, -- 1:1 物理锁死
    tool              TEXT    NOT NULL,             -- 工具名，如 "run_command" 或 "replace_file_content"
    args_pattern      TEXT,                         -- 参数正则 JSON，如 '{"CommandLine": "^git\\s+push"}'
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_tool ON tool_guards(tool);
-- 表达式唯一索引防 NULL 穿透
CREATE UNIQUE INDEX IF NOT EXISTS uidx_tg_rule ON tool_guards(tool, IFNULL(args_pattern, ''));

-- 5. 负向抑制边拓扑表（负向依赖边：inhibitor -> target；一票否决使能端）
-- 约束：target 必须为 'logic' 或 'guard'；inhibitor 必须为 'sensor' 或 'logic'
CREATE TABLE IF NOT EXISTS inhibitions (
    target_concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    inhibitor_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    created_at           TEXT    NOT NULL,
    PRIMARY KEY (target_concept_id, inhibitor_concept_id)
);
CREATE INDEX IF NOT EXISTS idx_inh_inhibitor ON inhibitions(inhibitor_concept_id);

-- 6. 时序状态机步进表（NFA 状态集：记录 CHAIN 逻辑当前活跃的步骤位置集合，支持重复/并发匹配）
CREATE TABLE IF NOT EXISTS active_chain_instances (
    chain_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    current_order    INTEGER NOT NULL DEFAULT 0,   -- 处于活跃状态的步骤编号 (1, 2, 3...)
    session_id       TEXT    NOT NULL,
    PRIMARY KEY (chain_concept_id, current_order, session_id)
);
CREATE INDEX IF NOT EXISTS idx_aci_session ON active_chain_instances(session_id);

-- 7. 审计与事后追踪日志表（纯事后只读审计，不参与 live 状态计算）
CREATE TABLE IF NOT EXISTS cli_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL DEFAULT 'default',  -- 当前工作会话 ID
    timestamp    TEXT    NOT NULL,
    command      TEXT    NOT NULL,                  -- 'fire', 'tool_call', 'sensor_trigger' 等
    concept_id   INTEGER,
    concept_name TEXT,
    sub_action   TEXT,
    success      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON cli_audit_log(session_id, timestamp);

-- 8. 当前活跃会话状态表 (Migration 006)
CREATE TABLE IF NOT EXISTS current_session (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 9. 会话待消费通知队列表 (Migration 007: 跨生命周期 Hook 消息暂存队列)
CREATE TABLE IF NOT EXISTS pending_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pn_session ON pending_notifications(session_id);
```

> **保留与配套表说明**：
> - `aliases` — 概念别名（`_resolve_id` 名字查找路径不变）
> - `tags`, `concept_tags` — 标签词表与概念-标签关联（标签回归纯元数据角色，结构原样保留）
> - `concepts.disclosure` — 1:1 纯文本书腰触发场景（直接挂在 concepts 表，极轻量标量）
> - `concept_embeddings` — 1:1 书腰嵌入向量表（冷热分离，`intent` 语义搜索专用附加表，1024 维 voyage-4-large）
> - `concept_transitions` — 注意力路由 `suggest` 数据源
> - `reminders` — 提醒系统（`remind` / `inbox` 命令依赖）
> - `schema_migrations` — 迁移版本记录
> - `v_compose` & `v_concept_tags` — GUI 可读视图保留并适配扁平概念模型
>
> 旧表中被彻底废除的只有 `variations` 表（DROP）及其关联查询。迁移由 `005_harness_v3.sql`、`006_current_session.sql`、`007_pending_notifications.sql` 平滑承接；`008_add_snapshots.sql` 另加 `snapshots` 表（CLI 改动快照，人工审核/回滚用）。

---

### 2.3 概念角色细则与生命周期

1. **普通砖块 (`plain`)**：纯静态知识、实体定义与教训手册，不持有任何激活规则与门禁，`is_active` 恒为 0，专门供 `read_concept` 查阅与 `suggest` 注意力联想。
2. **传感器与事实开关 (`sensor`)**：
   - **本体论铁律**：入度恒为 0，不接受任何上游计算或抑制干预。
   - **三级生命周期 (`lifespan`)**：
     - **`permanent`（长期环境开关 / 无法自动验证的底座事实）**：专门对应那些系统无法自动通过 Hook 感知/验证的客观环境与前提（如“硬件只读环境”、“已签署特定协议”）。**本体论死线：严禁绑定任何 `sensor_hooks`**。该开关跨 Session 长期有效，**是唯一允许由人工通过 CLI `set active/inactive` 显式手动拨动的传感器**。
     - **`session`（会话累积事实，默认值）**：能够通过事件感知自动验证的事实（如“已通过方案评审”、“已阅读手册”）。**本体论死线：必须通过 `sensor_hooks` 自动感知点亮，严禁手动 `set` 点亮作弊**。当前会话内单调为真，换新会话重置时归零，**强迫系统在每个新会话重新验证**。
     - **`turn`（单回合瞬态脉冲）**：能够通过事件感知捕获的瞬态信号（如用户本轮打回指令、单次工具报错）。**本体论死线：必须通过 `sensor_hooks` 自动感知点亮，严禁手动 `set` 点亮作弊**。仅当回合有效，轮次结束时自动清零。
3. **逻辑中继 (`logic`)**：
   - `activation_type` 必须为 `AND` / `OR` / `CHAIN`。
   - 自身电位是基于 Kahn DAG 拓扑排序计算出的物化派生缓存。
   - CHAIN 逻辑由 `active_chain_instances` 状态表跨回合锁存步进进度。
4. **放行守卫 (`guard`)**：
   - 管道出口处的守卫阀门。在 `tool_guards` 表中注册管辖规则。
   - **操作对称性与 1:1 物理独占**：与感知端的 `sensor` 节点严格 1:1 绑定单条 `sensor_hooks` 规则完全对称，**每个 `guard` 概念节点严格 1:1 独占单条 `tool_guards` 放行规则**（`guard_concept_id UNIQUE`）。一个肌肉阀门概念对应一个工具出入口。若需同时管控多个受控工具（如 `replace_file_content` 与 `write_to_file`），应在图谱中分别建立对应的守卫概念节点（如 `[代码修改_replace]` 与 `[代码修改_write]`），并让它们挂载相同的上游逻辑前置（扇出）或由组合逻辑收敛。
   - 其 `is_active == 1` 时放行工具，`is_active == 0` 时物理拦截。
   - **激活机制**：守卫的激活条件由它自身持有的 `compose_members` 上游输入与 `activation_type` 激活规则类型决定，与 `logic` 节点使用完全相同的求值公式。`activation_type` 为必填字段。即使只有单个上游成员，也必须显式指定 `activation_type`（AND 或 OR 均可，单元素时结果相同）。
   - **激活规则唯一性范围**：全局激活规则唯一性仅严格约束 `logic` 逻辑中继节点，杜绝无实体的同构复读机节点；**`guard` 节点作为物理效应器出入口，不受此唯一性限制**，允许不同的守卫概念节点绑定相同的激活规则（如 `[测试_放行_replace]` 和 `[测试_放行_write]` 均以 `[测试_实装准入条件]` 为触发条件）。`guard` 节点的排他性与冲突由 `tool_guards` 表的 `(tool, args_pattern)` 唯一性约束保障。

---

### 2.4 会话定位与多字段原子写入规范（Session & Atomic Mutation Spec）

1. **会话定位机制 (`session_id`)**：
   - **数据库持久化活跃会话 (`current_session` 表)**：不再依赖易失的内存或环境变量默认值。通过 `current_session` 表持久化记录当前工作会话 ID（由 Migration 006 引入）。
   - **外部宿主生命周期对齐**：IDE Hook 网关通过 `sync_session(db, conversation_id)` 严格对齐 IDE 当前活跃对话。当检测到新会话接入或会话切换时，自动调用 `db.init_session(session_id)`：覆写 `current_session`，物理清空历史残留的 `active_chain_instances` 与 `pending_notifications` 队列，并调用 `session_reset()` 复位临时会话与单轮电位并重算全图。
   - **离线脱机兜底 (`devonly`)**：若在脱机开发环境（如直接运行 CLI 命令）下未初始化活跃会话，底层统一回退至专用的 `OFFLINE_DEV_SESSION_ID = "devonly"`（定义于 `backend/_db_common.py`），并在 DB 中自动写入，保证 `active_chain_instances` 与 `cli_audit_log` 写入的一致性。

2. **CHECK 约束防死锁与复合更新机制**：
   - `create_concept` 原生支持 `--role`, `--lifespan`, `--activation_rule` 参数，单次 `INSERT` 时直接满足表级互斥 CHECK 约束。
   - 在 `_db_mutations.py` 中，角色切换与属性修改实行**原子复合更新**（避免单字段逐步更新被 CHECK 拦截报错）：
     - 切换为 `sensor` 时，若未指定 `lifespan` 自动赋予默认 `'session'`；
     - 设置 `activation_rule` 时，若当前概念为 `plain`，自动提升为 `logic`；
     - 降级为 `plain` 时，自动原子清空 `activation_type`, `lifespan`, `compose_members` 并将 `is_active` 置为 0。

3. **成员重复与时序回访规范**：
   - **`CHAIN`（时序序列）**：允许同一概念在不同步骤被多次回访（如 `A → B → A`，用于检测重试、反复修改、振荡或回路模式）。`compose_members` 仅以 `(parent_concept_id, order_index)` 为主键，天然支持时序回访。
   - **`AND` / `OR`（无序集合）**：属于数学集合逻辑，同一成员重复定义无意义（如 `A & A`）。去重校验由应用层 `_parse_activation_rule` / `_db_mutations.py` 负责拦截拒绝，不需要表级暴力去重。

4. **传感器生命周期、Hook 与手动点亮排异铁律**：
   - **可验证即重验（Verifiable $\to$ Ephemeral & Hook-driven）**：凡是能够通过 `sensor_hooks` 捕获事件进行验证的事实，其生命周期必须为 `session` 或 `turn`。生命周期的存在就是为了保证“每次新会话系统都会重新检查它”，**严禁人工手动作弊点亮**；
   - **不可验证即永久（Unverifiable $\to$ Permanent Manual Switch）**：`permanent` 专门用于系统无法自动验证的事实与环境底座，必须由人工显式手动拨动开关，**绝对严禁挂载 `sensor_hooks`**。
   - **写时与操作排异拦截（Mutation Gate）**：
     - 执行 `set active/inactive` 时，若目标传感器为 `lifespan != 'permanent'`（即 `session` 或 `turn`），直接 `reject` 并提示：“session/turn 传感器必须通过 sensor_hooks 自动感知激活，严禁手动点亮；只有 permanent 传感器允许手动拨动开关”；
     - 执行 `add sensor_hook` 时，若目标传感器为 `lifespan == 'permanent'`，直接 `reject` 并提示：“永久传感器不可绑定感知钩子；可自动验证的事实请将生命周期设为 session 或 turn”；
     - 执行 `set lifespan permanent` 时，若目标传感器已持有 `sensor_hooks`，直接 `reject` 并要求先解除 Hook 绑定。

---

## 3. 运行时计算与控制机制

### 3.1 DAG 拓扑排序求值引擎（Kahn Algorithm）

整个系统的信号传播建立在严格的有向无环图（DAG）上，正向边（`compose_members`）与负向边（`inhibitions`）共同参与 Kahn 排序与环路检测：

```text
外部事件触发 (Sensor 变更) 
   → 计算所有节点入度 (正向成员数 + 抑制源数) 
   → 入度为 0 的叶子节点入队 
   → 拓扑循环逐级推导计算 
   → 刷新 concepts.is_active 物化缓存
```

- **非叶子节点统一求值公式**（适用于 `logic` 和 `guard`，`sensor` 与 `plain` 不经此公式）：
  $$\text{new\_active} = \text{ActivationRule}(u) \land (\neg \exists \text{ inhibitor with is\_active == 1})$$
  - `AND`：所有正向成员（前置输入） `is_active == 1` 且无活跃抑制。
  - `OR`：任一正向成员（前置输入） `is_active == 1` 且无活跃抑制。
  - `CHAIN`：`active_chain_instances` 中存在 `current_order == MAX(order_index)` 且无活跃抑制。
- **绝无竞争**：下游节点获得计算机会的充要条件是所有上游输入均已出队计算完毕，数学上杜绝时序竞争。

### 3.2 时序逻辑状态机（CHAIN NFA 状态集）

- 当某节点 $X$ 发生 $0 \to 1$ 激活时，CHAIN 状态机按**波前合并**向前推进：
  - 若第 $k+1$ 步为 $X$，将分支迁移为 $k+1$（主键唯一性自动合并重叠分支，无冗余行）；
  - 若第 1 步为 $X$，插入起始状态 1；
  - 若任意分支跨入终点 $(N-1) \to N$，捕获一次终态迁移事件，触发该 CHAIN 的 `on_fire`。

### 3.3 声明式抑制（Inhibitions）

- **零成本恢复（Zero-Cost Recovery）**：抑制仅作为**求值门控（Gate）**将目标节点的 `is_active` 强制压为 0，**绝不回退上游传感器事实，也绝不清除 CHAIN 的步进状态**。一旦抑制源熄灭，目标节点在下一轮拓扑计算中直接依既有进度瞬间恢复为 1。
- **严格边界**：Target 只能是 `logic` 或 `guard`；Inhibitor 只能是 `sensor` 或 `logic`。

### 3.4 工具放行网关（Tool Guards & Hook Gateway）

在实际工程落地中，工具守卫判决收敛于 `backend/harness/core.py` 的 `guard(db, tool_name, tool_args, session_id)`，返回 `(allowed, deny_reason, fired_notifications)`；由 `backend/hook_gateway.py` 经各宿主适配器（antigravity / claude-code / codex）在 `PreToolUse` 事件调用：

1. **规则匹配**：查询 `tool_guards WHERE tool = :tool`，并将调用参数序列化后比对 `args_pattern` 正则。
2. **无人管（No Match）**：**默认放行（Default Allow）**。
3. **有人管（Matched）**：
   - 对应守卫 `is_active == 1` $\to$ **放行**；
   - 对应守卫 `is_active == 0` $\to$ **坚决拦截**，附带 `deny_reason`。
4. **深度因果溯源诊断**：
   拦截发生时，网关只检查守卫的**直接**成员与抑制源，不下钻（深层原因交给 `compile --target`，见 §4）：
   - 直接成员中有熄灭的传感器：输出 `传感器未点亮: <清单>`；
   - 直接成员中有未通电的 logic：输出 `前置节点未通电: <清单>（原因请运行 compile --target "<守卫>" 查看）`。logic 未通电可能是缺前置，也可能是被抑制，网关不替它下结论（2026-09-23 修正：此前一律写成"缺少必要前置"，被抑制时会误导）；
   - 以上两条是 AND 守卫的说法。OR 守卫写 `以下前置任一满足即可，目前都未满足: <清单>`；CHAIN 守卫写 `序列进行至第 k/N 步，等待: <成员>`，若该成员已处于点亮状态，则写明需要它重新发生一次（只有 0→1 才推进序列），不再误报为"前置已全部满足但守卫电位未同步"。实现见 `harness/core.py::_deny_reason`。
   - 若存在激活中的抑制源：输出 `当前被活跃抑制源 (<抑制源清单>) 强制锁死`；
   - 若守卫无任何前置：输出 `[系统配置错误] 该守卫未配置任何前置条件（孤岛死锁）`。
   最终构造标准拒绝报文：`工具 '<tool>' 调用已被拦截：受守卫 '<guard>' 管辖，<reason>。`，由各适配器按宿主协议包装后物理拦截。

> **操作对称与拓扑清晰（1:1 独占阀门设计）**：
> - **传入神经（Sensor）**：1 个 `sensor` 概念 $\leftrightarrow$ 1 条 `sensor_hooks` 规则（1:1 独占）。
> - **效应器肌肉阀门（Guard）**：1 个 `guard` 概念 $\leftrightarrow$ 1 条 `tool_guards` 规则（1:1 独占）。
> - **对称性带来的优势**：
>   1. CLI 与 API 操作语义高度统一（`set tool_guard` / `delete tool_guard` 均为原子单体覆盖/移除，无需按集合多重增删）；
>   2. 拓扑图谱中每个守卫节点都具象对应一个物理效应器出入口，工具与前置条件的扇入/扇出关系完全由概念图谱（`logic` / `compose_members` / `inhibitions`）显式表达，无隐藏的一对多黑盒。

### 3.5 电位持续性 vs. On-Fire 单发机制（State vs. Event）

系统严格区分 **持续电位（`is_active`）** 与 **单发动作（`on_fire`）** 两套正交维度：

1. **`on_fire` 恒为单次触发事件（One-Shot Pulse）**：
   - **触发独立性**：**无论节点的生命周期是 `turn`、`session` 还是 `permanent`，`on_fire` 动作永远只在状态达成的那一瞬间单发执行一次**，绝不会在后续轮次中重复发射。
   - **触发判据**：
     - **组合逻辑 / 传感器 / 守卫**：**电平上升沿检测（`0 → 1`）**。只有从未激活变为激活的当刻触发一次；之后只要保持为 1，绝不重复触发。
     - **时序逻辑（CHAIN）**：**终态迁移事件检测（$(N-1) \to N$）**。只有在某个分支跨入终点的那一刻发射一次。
   - **动作种类白名单与归一化（`VALID_ON_FIRE_ACTION_TYPES`）**：
     - 动作类型受严格白名单管控：`notify`（系统告警广播）、`set_focus`（注意力焦点引导）、`add_todo`（待办任务下发）；
     - 支持灵活输入形式：纯文本字符串（自动归一化包装为 `{"notify": "文本"}`）、单动作字典（`{"notify": "..."}`）或多动作列表（`[{"notify": "..."}, {"add_todo": "..."}]`）。
   - **红线**：`on_fire` 严禁包含修改电位的动作，仅用于注意力引导、提醒与通知等轻量外部副作用。

2. **求值返回协议与通知队列（`EvaluationResult` & `pending_notifications`）**：
   - 求值引擎在单趟拓扑计算后返回结构化变更与发火事件列表：
     ```python
     class FiredAction(BaseModel):
         concept: str
         concept_id: int
         action: Any

     class EvaluationResult(BaseModel):
         active_changed: dict[str, int] = {}
         fired_actions: list[FiredAction] = []
     ```
   - **跨阶段通知缓冲队列**：工具执行阶段（`PreToolUse`/`PostToolUse`）产生的 `notify` 无法在 IDE 工具通道直接输出给人类与模型，统一写入 `pending_notifications` 表暂存；在紧接着的 `PreInvocation` 阶段由 `db.pop_pending_notifications()` 批量消费，单点注入对话；
   - **信道物理隔离与防回音反射**：所有广播通知统一由 `wrap_harness_message()` 添加 `【Horon Harness】` 前缀封包；输入端传感器感知解析 transcript 时严格过滤该前缀，杜绝系统广播被误判为人类输入引发反射死循环。

3. **`is_active` 电位由生命周期与拓扑严格管理（Continuous State）**：
   - **`sensor`**：直接由自身的 `lifespan` 控制存活期（`turn` 当轮清零，`session` 会话重置清零，`permanent` 跨会话持久）。
   - **`logic (AND/OR)`**：无状态随动，其实际有效时长由上游有效输入的短板决定（上游含 `turn` 则当轮熄灭，上游全为 `session` 则持续通电）。
   - **`logic (CHAIN)`**：由 `active_chain_instances` 状态表跨回合锁存进度，终态电位在当前 Session 内持续有效。
   - **`guard`**：作为出口阀门，电位随动于其上游逻辑与抑制状态。

4. **生命周期自动清理时机与主权边界**：
   - **AI 无权主动重置 Harness 追踪状态**：CLI 严禁提供任何 `reset` 命令。Harness 的状态反映的是客观物理事实与历史轨迹，受控的 AI 绝不能拥有擦除自身被拦截状态或推演进度的权限。
   - **宿主生命周期钩子（Host Lifecycle Hooks）**：
     - **单回合结束 (`Turn End`)**：由外部宿主/网关在 `Stop` 事件时调用内部方法 `turn_end()`：`UPDATE concepts SET is_active = 0 WHERE lifespan = 'turn'` $\to$ 触发全图拓扑重算，随动熄灭所有依赖瞬态输入的下游节点。
     - **会话重置 (`Session Reset`)**：由外部宿主在检测到 `conversationId` 切换时调用 `init_session()`：覆写活跃会话，清空 CHAIN 状态机与未消费通知，重置 `turn` 与 `session` 电位 $\to$ 触发全图拓扑重算。

---

## 4. 编译器：目标逆推差集求解器（Backward Solver）

> [!NOTE]
> **当前实装状态**：**已实装（2026-09-23）**，代码在 `backend/_db_compile.py`，CLI `compile --target` 已导通，输出由 `frontend/cli.py::_format_compile` 渲染。
> 旧版 `compile`（`--assume`, `--block`, `--constraints`, `--goal`）的 BFS/DFS 证明引擎 `backend/compiler.py` 已无任何引用，待删除。

- **命令**：`python frontend/cli.py compile --target <目标概念> [--assume <额外假设激活的节点>...]`
- **语义**：给定当前图的实际电位状态（加上 `--assume` 额外假设激活的节点），回答"目标节点为什么没通电 / 已经通电"。`--assume` 在当前已激活节点基础上叠加额外假设，用于规划"如果我再点亮 X 和 Y，目标能通电吗？还差什么？"。不再有路径搜索——编译器沿依赖树向下读取并诊断。
- **`--assume` 限制**：只接受 `sensor`。logic/guard 的电位由规则推导，假设它们亮等于绕过规则；要假设请假设其上游传感器。目标为 `plain` 时报错（无电位可诊断）。
- **算法**：
  1. 在 SQLite `SAVEPOINT` 内按 `--assume` 给出的顺序逐个点亮传感器，每个都交给真实 `GraphEvaluator.evaluate()` 单独求值一趟（假设的语义是"依次发生"，CHAIN 按这个顺序推进），读出假设下的全图电位与 CHAIN 进度，然后 `ROLLBACK`。诊断与运行时引擎逐位一致（包括 CHAIN 推进），且 compile 不写任何表。
  2. 从目标沿 `compose_members` 向下递归至熄灭的叶子传感器，沿途检查活跃 `inhibitions`，得到差集：AND 递归展开并逐项列出（去重）；OR 合并为一项 `A 或 「L」`；嵌套 CHAIN 写为 `CHAIN「C」第 k/N 步需要 X（之后还有 m 步）`。OR 选项和嵌套 CHAIN 的等待步骤只要是 logic，就写成「名字」，不内联它的子树（内联会让共享子图的文本按指数膨胀），细节交给诊断树展开，每个节点只展开一次。节点被活跃抑制时，无论规则是否满足，都追加"解除「X」的抑制"。等待节点已为 1 时，提示需要一次新的上升沿。plain 成员、无前置、CHAIN 步骤编号不连续等标为 `[配置错误]`。差集按节点记忆化，共享节点只算一次。
  3. 同时生成依赖诊断树：熄灭节点向下展开，通电节点只在根部展开一层；熄灭的传感器附带点亮方式（等待哪个 hook / permanent 需人工 `set active` / 无 hook 的配置错误）；`--assume` 点亮的节点标 `(假设)`。
- **输出（`CompileResult.status`）**：
  - `unmet_prerequisites` → `[✗ 缺少前置]`；目标为 CHAIN 时为 `[⏳ 序列等待] 进行至第 k/N 步，等待 Z`；
  - `inhibited` → `[⛔ 抑制锁死]`（目标自身规则已满足，但被直接抑制源压为 0）；
  - `active` → `[✓ 导通放行]`。

---

## 5. 旧结构清理与解耦

### 5.1 结构级废除

1. **废除变体（Abolish Variations）**：DROP `variations` 表；重构 `v_compose` 视图以直接映射 `compose_members` 扁平概念模型（保留供 GUI 查阅）。多种激活路径拆解为具名子概念（logic 节点）并通过 `OR` 逻辑汇聚。所有以 `concept:short_code` 形式定位节点的接口全部移除。
2. **废除认知状态系统**：`status`（`hypothesis` / `confirmed` / `negated`）整体移除。节点唯一的运行态是 `is_active`（0 或 1）。连带移除：
   - `audit_status_integrity()`（级联降级审计——无 status 则无审计对象）
   - `_set_status()` 方法与 CLI `set status` 命令
   - 编译器中对 status 的一切引用（旧编译器按 confirmed/hypothesis 过滤路径）
3. **废除 valence 列**：`variations.valence` 从未实际写入，随 `variations` 表一并移除。`models.py` 中 `Variation.valence` 与 `DirectedRelation.valence` 字段删除。
4. **消除冗余关系节点**：废除 `A->B` 包装节点，依赖直接内聚在 compose_members 中。
5. **废除同级有向出入边与关系模型（Abolish Inbound/Outbound Directed Relations）**：彻底移除 `inbound_relations` / `outbound_relations` / `DirectedRelation` / `RelationMember`。消除将 `CHAIN` 中前后项强行包装为平级出入边的旧图谱范式。注意力跳转由 `suggest` (`concept_transitions`) 与 `on_fire` (`set_focus`) 承接；控制依赖由 `compose_members` (Kahn DAG) 与 `inhibitions` 承接。彻底删除 `_query_directed_relations` 及其派生查询。

### 5.2 角色与验证迁移

5. **废除标签拓扑特权**：移除 `_validate_expression_context()`（旧版要求持有 state/action/result 标签才能定义规则）。在新架构中，激活规则容纳能力由 `role` 字段严格管控（仅 `logic` 与 `guard` 可持有 compose_members），标签回归纯元数据分类。
6. **旧编译器替换**：逆推差集求解器已在 `backend/_db_compile.py` 实装（见 §4），`_load_relation_graph()` 已移除。旧 `backend/compiler.py`（~480 行 BFS/DFS 证明搜索引擎）不再被引用，待删除。

---

## 6. 一次性端到端行为验收

> [!NOTE]
> **定位说明**：Horon 引擎本身是**领域无关（Domain-Agnostic）的通用基础设施**，底层不内置任何具体业务概念。
> 本节电路只用于当前实施阶段的一次性手工验收，跑通全链路（传感器感知 → 逻辑求值 → 网关门禁 → 抑制压制 → 编译器逆推诊断）后即清理现场，不写入或保留任何持久测试文件。

### 6.1 验收场景

```text
[测试_已阅读手册] (sensor hook 自动点亮, role='sensor')
        &
[测试_已建立大纲] (sensor hook 自动点亮, role='sensor')
        &
[测试_大纲已过审] (sensor hook 自动点亮, role='sensor')
        ↓ (AND 组合, compose_members)
[测试_实装准入条件] (logic, 自动求值 is_active=1, role='logic')
        ├─→ (1:1 绑定 replace_file_content) [测试_放行_replace] (guard) → 放行 replace_file_content
        └─→ (1:1 绑定 write_to_file)        [测试_放行_write] (guard)   → 放行 write_to_file
```

**抑制分支验收：**
```text
[测试_大纲被拒] (sensor hook 捕获外界打回消息, is_active=1, role='sensor')
   └─→ inhibitions 表：[测试_大纲被拒] 抑制 [测试_实装准入条件] 或各 [测试_放行_*] 守卫
   └─→ 只要 [测试_大纲被拒] 保持 is_active=1，目标持续被压制为 0
   └─→ 两个受控工具网关持续断电 (写代码工具被拦截锁死，直到打回状态被清除)
```

### 6.2 当轮行为检查

1. **初始拦截（Default Block）**：初始电位为 0，调用 `guard(db, "replace_file_content", {...})`（`backend/harness/core.py`）断言 `allowed == False` 且拦截信息包含未激活守卫 `测试_放行_replace`。
2. **差集诊断（Compiler Diagnostics）**：仅点亮前两个传感器时，执行逆推求解 `compile --target "测试_放行_replace"` 断言精准输出缺少前置 `测试_大纲已过审`。
3. **导通放行（Circuit Conduction）**：三前置全部点亮后，Kahn 求值使两个守卫激活为 1，断言两个受控工具调用均返回 `True` 放行。
4. **抑制生效（Inhibition Override）**：点亮 `测试_大纲被拒` 后，即使三前置依然为 1，断言目标节点被强制压制为 0，工具调用再次被锁死。
5. **重置归零（State Reset）**：调用底层宿主方法 `session_reset()` 后，确认所有会话/单轮电位与状态恢复初始 0。

这些检查可以使用当轮临时脚本执行，但脚本不得提交或跨任务保留；验收结束后删除。

> **验收记录（2026-09-23）**：五项全部通过（临时库 + 临时脚本，已删除）。另外验证了 OR 合并、CHAIN 步进与重访、permanent 提示、`--assume` 非 sensor 报错，以及 compile 在真库上执行前后电位 / CHAIN / 会话状态哈希一致（零副作用）。

---

## 7. 实施路线（分步交付与审查）

> [!IMPORTANT]
> **执行与协作纪律**：
> 1. 严格按照 **Step 1 $\to$ Step 8** 顺序推进，不跳步、不抢跑。
> 2. **每完成一个 Step，主动停下向 Salem 汇报代码改动（diff 摘要）与运行效果，得到确认后再启动下一步。**
> 3. `schema.sql`（全新库初始化）与 `backend/migrations/005_harness_v3.sql`（现有库迁移）在 **Step 1 同步交付**，保证新旧数据库行为从第一天起完全一致。
> 4. 以第 6 节的一次性行为检查结果作为阶段一的核心闭环验收依据；仓库不建立持久测试套件。

---

### 阶段一：底层数据、迁移与核心引擎

#### Step 1: [✓ 已完成] Schema 改造、SQL 迁移脚本与基础 DB 读写
- **改动范围**：
  - `backend/schema.sql` (全新库 DDL)
  - `backend/migrations/005_harness_v3.sql` (现有库升级迁移脚本)
  - `backend/models.py`
  - `backend/db.py` (resolution 逻辑重写：废除 `_resolve_single_variation`, `_next_short_code` 等 variation 耦合方法，`_resolve_id` 简化为纯概念定位)
  - `backend/_db_concepts.py`, `backend/_db_mutations.py`
  - `backend/_db_query.py` (`read_concept`, `search_concepts`, `get_all_concepts_overview` 全部依赖 `variations` 表 JOIN，必须同步重写为扁平概念查询)
  - `backend/_db_compile.py` (`_load_relation_graph()` 直接查 `variations` 表，Step 1 先 stub 为 `raise NotImplementedError`，已于 Step 3 重写为逆推求解器)
  - `backend/_db_plugins.py` (移除 variations 表查询，使用扁平 ConceptProxy 适配)
  - `backend/tag_sandbox.py` (重构 Proxy 交互模型，移除与 variations 的旧绑定)
- **实现内容**：
  - `schema.sql`：落地扁平 `concepts`（含 `role`, `is_active`, `lifespan`, `activation_type`, `on_fire` 及互斥约束）及 `sensor_hooks`, `tool_guards`, `inhibitions`, `active_chain_instances` 表；
  - `005_harness_v3.sql`：编写结构升级与旧数据平滑转换 SQL：
    - 持有 `compose_members` 的旧概念 $\to$ `role = 'logic'`, `activation_type = type`；
    - 被作为 member 引用且持有 `state`/`action`/`result` 标签的叶子概念 $\to$ `role = 'sensor'`, `lifespan = 'session'`；
    - 其余孤立/纯描述概念 $\to$ `role = 'plain'`, `activation_type = NULL`, `lifespan = NULL`；
    - 保留别名、标签、书腰与审计日志。
  - `models.py`：定义 Pydantic/Dataclass 数据模型（使用 `activation_type`, `activation_rule` 替代旧字段）；
  - `db.py`：重写概念定位基础设施，移除 variation 解析路径；
  - `_db_concepts.py` / `_db_mutations.py`：实现新表的基础增删改查（`set_active`, `set_lifespan`, `set_activation_rule`, `add_compose_member`, `add_inhibition`, `set_tool_guard`, `set_sensor_hook`，以及防 CHECK 死锁的原子复合更新）；
  - `_db_query.py`：重写 `read_concept` / `search_concepts` / `get_all_concepts_overview` 为扁平概念查询（不再 JOIN `variations`）；
  - `_db_plugins.py` / `tag_sandbox.py`：同步移除 variation 代理。
- **审查卡点**：用一次性脚本验证全新库创建与现有 `horon.db` 迁移后 Schema 完全一致、旧数据无损；检查后删除脚本。

#### Step 2: [✓ 已完成] 核心求值引擎（DAG 拓扑 + CHAIN 时序 + 抑制压制 + on_fire）
- **改动范围**：`backend/evaluator.py`, `backend/chain_engine.py`（或整合至相关模块）
- **实现内容**：
  - 实现基于 Kahn 拓扑排序的 DAG 单趟求值器（含环路检测、AND/OR 门控）；
  - 实现 CHAIN NFA 跨会话时序步进器（按序匹配推进、终态达成检测）；
  - 实现抑制门控（Inhibition Gate）：活跃抑制源强制将目标电位压制为 0；
  - 实现 `on_fire` 上升沿（0→1）单发与终态单发检测；
  - 落地 `EvaluationResult` 结构化返回协议。
- **审查卡点**：检查拓扑求值无死循环、时序状态机无状态污染、抑制优先级最高。

#### Step 3: [✓ 已完成] 工具网关、逆推求解器与端到端闭环
- **改动范围**：`backend/harness/core.py` + `backend/hook_gateway.py`（替代原计划独立的 `gatekeeper.py`）, `backend/_db_compile.py`
- **已实装内容（以实际代码为准）**：
  - **工具守卫拦截**：`backend/harness/core.py::guard(db, tool_name, tool_args, session_id)`，经各适配器挂载于宿主 `PreToolUse`。守卫 `is_active == 0` 时检查其直接成员（`compose_members`）与活跃抑制源（`inhibitions`）给出拒绝理由；未通电的 logic 成员指路到 `compile --target <守卫>`。
  - **逆推差集求解器**：`backend/_db_compile.py::compile(target, assume)`，算法与输出见 §4。
  - **§6 端到端验收**：2026-09-23 五项通过。
- **审查卡点**：完成逆推求解器编写，跑通第 6 节全项检查，确认全链路物理闭环。

---

### 阶段二：CLI 前端命令升级

#### Step 4: [✓ 已完成] CLI 适配与旧命令清理
- **改动范围**：`frontend/cli.py`
- **已落地内容**：
  1. **`read_concept` 专属电路视图落地**：
     - **彻底拔除** `Activation Rule: (None / Atomic)`、`Expression: ...` 等旧版残留；
     - 按 `role` 渲染专属电路分区：
       - `role = 'logic' | 'guard'`：展示 `[ ACTIVATION RULE / 激活规则 ]`（类型、公式及输入成员实时电位 `[1 / 通电]` / `[0 / 灭]`；CHAIN 序列展示按序步骤）；
       - `role = 'sensor'`：展示 `[ SENSOR / 外部事实源 ]`（生命周期 `lifespan` 属性说明与绑定的 `sensor_hooks`）；
       - `role = 'plain'`：展示 `[ PLAIN / 静态知识 ]`；
     - 概念头部呈现物理电位 `Active: [1 / 通电]` 或 `[0 / 灭]`；
     - 独立呈现 `[ TOOL GUARDS / 工具放行门禁 ]` 与 `[ INHIBITIONS / 抑制关系 ]`（含抑制源实时通断状态）。
  2. **`list_concepts` 概览优化**：直观展示每个概念的角色（role）、实时电位（1/通电, 0/灭）及激活规则类型。
  3. **命令族全面对齐 Harness v3**：
     - `set activation-rule` / `delete activation-rule` — 规则定义与原子降级；
     - `set active/inactive` — 手动切换永久环境传感器电位（**仅限 `lifespan == 'permanent'`**，`session`/`turn` 强制由 Hook 驱动）；
     - `set lifespan` / `set role` / `set on_fire` — 属性与发火动作配置；
     - `set sensor_hook` / `delete sensor_hook` — 1:1 独占感知规则绑定/解绑；
     - `set tool_guard` / `delete tool_guard` — 1:1 独占守卫规则绑定/解绑；
     - `add inhibition` / `delete inhibition` — 负向抑制边增删；
     - `delete <target>` — 默认删除概念本体。
  4. **旧代码彻底清理**：移除旧 BFS/DFS 路径搜索路线格式化函数（`_format_route`, `_format_segments_chain`）及过时提示。
  5. **`compile` CLI 接口升级为新规范**：
     - 参数切换为 `compile --target <目标概念> [--assume <额外假设>...]`；
     - 编写结构化逆推诊断输出格式化器 `_format_compile`（就绪/抑制锁死/缺少前置）。
- **求解器回接**：`compile --target` 已导通（2026-09-23），`[Step 3 Pending]` 占位分支已删除。
- **审查卡点**：实际运行 CLI 常用命令，确认 `read_concept` 专属电路分区渲染与电位显示准确、旧命令清理无残留。

#### Step 5: [✓ 已完成] 废除同级有向出入边与关系查询模型彻底清理（Abolish Directed Relations & Outbound/Inbound Query Cleanup）
- **改动范围**：
  - `backend/models.py` (删除 `DirectedRelation`, `RelationMember`，从 `ReadResult` 中移除 `inbound_relations`, `outbound_relations`)
  - `backend/_db_query.py` (删除 `_query_directed_relations`, `_query_inbound_relations`, `_query_outbound_relations`，重构 `read_concept` 结构)
  - `frontend/cli.py` (彻底移除 `[ INBOUND ]` / `[ OUTBOUND ]` 显示分区，注意力完全收敛至 `[ YOU MAY ALSO NEED ]` / `suggested_next`)
  - `backend/server.py` & `web/src/types.ts` (同步移除 API 和类型定义中的旧 relations 残留)
- **实现内容**：
  - 彻底清除代码库中将 `CHAIN` 前后项强行解释为“同级有向边（Inbound/Outbound）”的过渡期残留；
  - 严格贯彻“注意力的归注意力（`suggest` + `on_fire`）、逻辑控制的归逻辑控制（`compose_members` + `inhibitions`）”的架构分工；
  - 拔除 DB 层无意义的 `_query_directed_relations` 自连接查询，提升 `read_concept` 吞吐效率。
- **审查卡点**：实际运行 `read_concept`，确认输出无出入边残留，并扫描生产代码确认没有旧关系依赖。

---

### 阶段三：Web 可视化前端与 FastAPI 服务升级

#### Step 6: [✓ 核心已完成 / 星图画布待增强] Web 可视化前端适配与纯只读注意力跳跃（Web UI & Read-only Navigation）
- **改动范围**：
  - `backend/server.py` (FastAPI 接口层)
  - `web/src/types.ts` (前端 TypeScript 类型定义)
  - `web/src/components/InspectorSidebar.tsx` / `InspectorSidebar.css` (侧边栏检查器)
  - `web/src/components/DissectionView.tsx` / `DissectionView.css` (概念聚焦/解剖视图)
  - `web/src/components/GalaxyView.tsx` (全景星图/电路图)
- **已实装内容**：
  1. **零副作用与只读隔离铁律（Pure Read-only Exploration）**：已确认。`backend/server.py` 的 GET 查询均不写入 `cli_audit_log` 且不触碰 `concept_transitions`；
  2. **注意力引导跳跃（Attention-Guided Hops via `suggest`）**：Inspector 侧边栏已完整实现 `[ SUGGESTED NEXT ]` 权重列表展示与平滑焦点切换；
  3. **Harness 节点角色与电位可视化**：
     - `web/src/types.ts` 全量更新，拔除 `variations` 与旧出入边关系；
     - `InspectorSidebar` 完整渲染角色徽章（plain/sensor/logic/guard）、实时电位（Active/Inactive）、生命周期、激活规则、发火动作、感知钩子、工具守卫与抑制链；
     - `DissectionView` 适配扁平概念模型、组合成员内部关系、有向/无向/抑制连线；
  4. **局部电路拓扑（Neighborhood Circuit View）**：重构 `/api/neighborhood/{id}`，返回中心概念成员、上游汇聚父概念与双向抑制边。
- **细微未尽项**：
  - `GalaxyView.tsx` 2D Canvas 上的节点颜色当前仍由连通度（degree）渐变驱动，尚未映射角色（role）专属主题色系与电位（is_active）发光动画（侧栏已支持，星图画布可作为视觉润色项增强）。
- **审查卡点**：启动 Vite 与 FastAPI，在浏览器中打开 Web 界面，验证星图/解剖视图渲染正常，能够通过 suggest 卡片点击跳跃，且全过程不向 `concept_transitions` 写入任何记录。

---

### 阶段四：插件清理与系统文档

#### Step 7: [✓ 已完成] 插件清理与 Agent 技能说明书更新
- **改动范围**：`backend/_db_plugins.py`, `backend/tag_sandbox.py`, `backend/tag_plugins/`, `.agents/skills/horon-cli/SKILL.md`, `.agents/skills/horon-plugin/SKILL.md`, `README.md`
- **已实装内容**：
  - `backend/_db_plugins.py` 与 `backend/tag_sandbox.py` 彻底移除对 `variations` 的依赖，使用扁平 `ConceptProxy` 交互模型；
  - 清理系统标签插件（`state.py`, `action.py`, `result.py`, `exit.py`）中对旧变体与拓扑特权的硬编码；
  - 全面更新 `.agents/skills/horon-cli/SKILL.md` 与 `.agents/skills/horon-plugin/SKILL.md`，文档与 Harness v3 CLI/插件机制完全对齐；
  - 更新项目顶层 `README.md`。
- **审查卡点**：审查 Skill 文档与说明书，确认所有命令与语法均为最新。

---

### 阶段五：物理 Hook 接入（生产装配）

#### Step 8: [✓ 已完成 / 超前交付] 物理 Hook 接入与 IDE 全生命周期网关
- **改动范围**：`backend/hook_gateway.py`, `backend/harness/core.py`, `backend/harness/adapters/{antigravity,claude_code,codex}.py`, `.agents/hooks.json`, `.claude/settings.json`
- **已实装内容（较原计划有大幅架构升级，以实际代码为准）**：
  - **超越单一工具拦截的全生命周期网关**：原计划仅设计 Pre-Tool Hook，实际实现了覆盖 Antigravity IDE 5 大生命周期阶段的物理网关：
    1. **`PreInvocation`**：会话对齐（`sync_session`）+ 消费并注入暂存通知队列（`pop_pending_notifications`）+ 感知人类输入（`user_message` 传感器）+ `wrap_harness_message` 封包广播；
    2. **`PreToolUse`**：感知工具调用（`tool_call` 传感器）+ 守卫门禁硬拦截（`harness/core.py::guard`，未通电物理 `deny`，已通电 `allow`）；
    3. **`PostToolUse`**：感知工具执行结果与报错（`tool_result` 传感器）+ 结果分析暂存；
    4. **`PostInvocation`**：审查模型回复言论（`model_message` 传感器，拦截寄生认同/空头承诺/自虐词汇）+ 注入警告并设置 `force_continue` 强迫模型当轮修正；
    5. **`Stop`**：停机前积压通知兜底拦截 + 单回合结束调用 `turn_end()` 熄灭 `turn` 传感器并拓扑重算。
  - **多宿主适配**：以上 5 个阶段是 Antigravity 的事件名；另有 Claude Code CLI 与 Codex CLI 适配器，把各自的生命周期事件映射到同一组核心动作（`sense` / `guard` / `drain` / `sync_session` / `end_turn`）。
  - **宿主配置已就绪**：入口统一为 `python -m backend.hook_gateway <EventName> [--adapter antigravity|claude-code|codex]`（未指定时按环境变量与 payload 特征自动识别）；Antigravity 配置在 `.agents/hooks.json`，Claude Code 配置在 `.claude/settings.json`。
- **审查卡点**：在真实会话中触发高危调用，确认物理拦截与放行符合预期。

---

## 8. 尚未实装清单与完成状态核对（Completion Status & Final Audit）

截至 2026-10-02 核对：**Step 1–8 全部交付，§6 验收全项通过，后续待办已全量结案。**

### 8.1 清理与数据修正
1. **[✓ 已完成] 删除旧 `backend/compiler.py`**：已于 2026-09-23（commit `b53c2657`）彻底删除，无任何悬挂引用。
2. **[✓ 已完成] 修正违反角色契约的真库数据与写入校验**：
   - 2026-09-26（commit `f366355c`）在 `backend/db.py` 中引入 `_check_circuit_members` 与 `_check_role_change_keeps_members_valid`，在概念创建、修改规则、切换角色等所有写入入口严格拒绝 `plain` 节点充当电路成员；
   - 真库数据已完成洗牌与修正，所有逻辑前置均转换为标准 `sensor`（如「独立推理通道就绪」等）；
   - 当前在真实数据库运行 `audit_db_integrity()` 报告 `Circuit Contracts: All composition members are circuit nodes (sensor/logic) ✓`，违规数量为 0。

### 8.2 功能与展示现状说明
1. **全景星图（`GalaxyView.tsx` 2D Canvas）展示分工**：
   - 全景星图保留按 `byte_size`（节点文本体积）呈现恒星色温光谱热度图与超重红色警示圈的设计，用于宏观监控节点膨胀态势；
   - 节点的 `role`、`is_active`、`lifespan`、`activation_type` 均已由 `/api/graph` 全量返回；
   - 角色的详细分类、通电/断电态势、组合逻辑规则、感知钩子与工具放行守卫已在侧边栏检查器（`InspectorSidebar`）与解剖视图（`DissectionView`）中完整渲染。

---

## 9. 结论

Horon Harness 系统的认知控制与物理拦截重构（Step 1 ～ Step 8）及全部配套清理工作已彻底完工，逆推求解器、各宿主 Hook 网关与数据库契约约束均已稳定运转。

本重构计划文档已完成其指引与追踪使命。
