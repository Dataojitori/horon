-- ============================================================
-- Horon v3 Harness Schema: Executable Cognitive Harness
--
-- Concepts are flat entities with designated roles:
--   'plain':  Static knowledge, entities, definitions (is_active = 0)
--   'sensor': External facts / perceptual inputs (lifespan IN ('turn', 'session', 'permanent'))
--   'logic':  Combinational & sequential logic (activation_type IN ('CHAIN', 'AND', 'OR'))
--   'guard':  Tool gateway gating valve (activation_type IN ('CHAIN', 'AND', 'OR'))
-- ============================================================

-- 1. 概念主表
CREATE TABLE IF NOT EXISTS concepts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    content         TEXT,                           -- 描述、事实、证据笔记
    role            TEXT    NOT NULL DEFAULT 'plain'-- 'plain'(砖块), 'sensor'(传感器), 'logic'(逻辑中继), 'guard'(放行守卫)
                            CHECK(role IN ('plain', 'sensor', 'logic', 'guard')),
    
    -- 电位状态（直接挂在概念上，单点读写；反映当前节点的客观物理电位：0=灭/断电, 1=亮/通电）
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),     
    
    -- 传感器保鲜期 (仅 role = 'sensor' 有效；非传感器恒为 NULL)
    lifespan        TEXT    CHECK(lifespan IS NULL OR lifespan IN ('turn', 'session', 'permanent')),
    
    -- 激活规则类型 (仅 role = 'logic' 或持有激活规则的 'guard' 有效；sensor 与 plain 恒为 NULL，绝无上游计算依赖)
    activation_type TEXT    CHECK(activation_type IS NULL OR activation_type IN ('CHAIN', 'AND', 'OR')),
    
    -- 发火动作配置 (JSON 文本，用于注意力引导等轻量副作用)
    -- 例：{"set_focus": "代码实装规范"}
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

-- 4. 输出端：工具放行守卫规则表（效应器 / 工具拦截与放行网关）
CREATE TABLE IF NOT EXISTS tool_guards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guard_concept_id  INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE, -- 1:1 物理锁死
    tool              TEXT    NOT NULL,             -- 工具名，如 "run_command" 或 "replace_file_content"
    args_pattern      TEXT,                         -- 参数正则 JSON，如 '{"CommandLine": "^git\\s+push"}'
    created_at        TEXT    NOT NULL
);

-- 5. 负向抑制边拓扑表（负向依赖边：inhibitor -> target；一票否决使能端）
-- 约束：target 必须为 'logic' 或 'guard'；inhibitor 必须为 'sensor' 或 'logic'
CREATE TABLE IF NOT EXISTS inhibitions (
    target_concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    inhibitor_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    created_at           TEXT    NOT NULL,
    PRIMARY KEY (target_concept_id, inhibitor_concept_id)
);

-- 6. 时序状态机步进表（NFA 状态集：记录 CHAIN 逻辑当前活跃的步骤位置集合，支持重复/并发匹配）
CREATE TABLE IF NOT EXISTS active_chain_instances (
    chain_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    current_order    INTEGER NOT NULL DEFAULT 0,   -- 处于活跃状态的步骤编号 (1, 2, 3...)
    session_id       TEXT    NOT NULL,
    PRIMARY KEY (chain_concept_id, current_order, session_id)
);

-- 7. 审计与事后追踪日志表（纯事后只读审计，不参与 live 状态计算）
CREATE TABLE IF NOT EXISTS cli_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL DEFAULT 'default',                  -- 当前工作会话 ID
    timestamp    TEXT    NOT NULL,
    command      TEXT    NOT NULL,                  -- 'fire', 'tool_call', 'sensor_trigger' 等
    concept_id   INTEGER,
    concept_name TEXT,
    sub_action   TEXT,
    success      INTEGER NOT NULL DEFAULT 1
);

-- 8. 别名表
CREATE TABLE IF NOT EXISTS aliases (
    alias       TEXT    PRIMARY KEY,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE
);

-- 9. Tag 词表注册表与关联表
CREATE TABLE IF NOT EXISTS tags (
    name              TEXT PRIMARY KEY,
    source_concept_id INTEGER        -- NULL = system tag
);

-- 种子词表（系统保留 tag）
INSERT OR IGNORE INTO tags (name) VALUES ('result'), ('exit'), ('state'), ('action');

CREATE TABLE IF NOT EXISTS concept_tags (
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    tag         TEXT    NOT NULL REFERENCES tags(name) ON UPDATE CASCADE,
    PRIMARY KEY (concept_id, tag)
);

-- 10. Multi-Disclosure: 书腰（触发条件），每个 concept 可以有多条
CREATE TABLE IF NOT EXISTS disclosures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    text            TEXT    NOT NULL,
    embedding       BLOB,
    embedding_model TEXT,
    created_at      TEXT    NOT NULL
);

-- 11. Attention Routing: 概念间的注意力转移突触权重
CREATE TABLE IF NOT EXISTS concept_transitions (
    from_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    to_concept_id   INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    weight          REAL    NOT NULL DEFAULT 0.0,
    last_accessed_at TEXT   NOT NULL,
    PRIMARY KEY (from_concept_id, to_concept_id)
);

-- 12. Reminder 系统（轻量级提醒与收件箱）
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    condition     TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_fired_at TEXT
);

-- 13. 迁移版本记录
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- 索引集合
CREATE INDEX IF NOT EXISTS idx_cm_member          ON compose_members(member_concept_id);
CREATE INDEX IF NOT EXISTS idx_sh_lookup           ON sensor_hooks(event_type, tool);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_sh_event   ON sensor_hooks(event_type, IFNULL(tool, ''), match_pattern);
CREATE INDEX IF NOT EXISTS idx_tg_tool            ON tool_guards(tool);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_tg_rule    ON tool_guards(tool, IFNULL(args_pattern, ''));
CREATE INDEX IF NOT EXISTS idx_inh_inhibitor      ON inhibitions(inhibitor_concept_id);
CREATE INDEX IF NOT EXISTS idx_aci_session        ON active_chain_instances(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_session      ON cli_audit_log(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_al_concept         ON aliases(concept_id);
CREATE INDEX IF NOT EXISTS idx_concept_tags_tag   ON concept_tags(tag);
CREATE INDEX IF NOT EXISTS idx_disclosures_concept ON disclosures(concept_id);
CREATE INDEX IF NOT EXISTS idx_reminders_concept  ON reminders(concept_id);

-- GUI 可读视图
CREATE VIEW IF NOT EXISTS v_compose AS
SELECT
    cm.parent_concept_id AS concept_id,
    c1.activation_type   AS activation_type,
    c1.name              AS concept_name,
    cm.member_concept_id,
    c2.name              AS member_concept_name,
    cm.order_index
FROM compose_members cm
JOIN concepts c1 ON cm.parent_concept_id = c1.id
JOIN concepts c2 ON cm.member_concept_id = c2.id
ORDER BY cm.parent_concept_id, cm.order_index, cm.member_concept_id;

CREATE VIEW IF NOT EXISTS v_concept_tags AS
SELECT
    ct.concept_id,
    c.name AS concept_name,
    ct.tag
FROM concept_tags ct
JOIN concepts c ON ct.concept_id = c.id
ORDER BY ct.tag, c.name;
