-- ============================================================
-- Horon v2 Schema: Concept → Variation 分层结构
--
-- Concept:   概念的对外身份（名字层），组合的参与单位。
-- Variation: 同一概念的不同解释（concept_id + short_code），
--            每个 variation 有独立的 compose_members / status / content。
-- compose_members 的 member 引用 concept_id（hub），不是具体 variation。
-- ============================================================

CREATE TABLE IF NOT EXISTS concepts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    disclosure  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS variations (
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    short_code  TEXT    NOT NULL,
    type        TEXT    CHECK(type IN ('CHAIN', 'AND', 'OR')),
    status      TEXT    CHECK(status IN ('hypothesis', 'confirmed', 'negated')),
    content    TEXT,
    unless      TEXT,
    -- 价值通道（与因果内容正交，编译器对此列全盲）：
    -- 这条 variation 被现实碰撞后对我的利害。NULL=从未审视，
    -- -1.0=harmful, 0.0=neutral, +1.0=beneficial。
    -- 列用 REAL 保持存储通用；写入口（CLI）只接受符号标签、不接受裸数字，
    -- 幅度将来若获得合法刻度来源（如读取端聚合缓存）再开闸。
    valence     REAL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (concept_id, short_code)
);

CREATE TABLE IF NOT EXISTS compose_members (
    concept_id        INTEGER NOT NULL,
    short_code        TEXT    NOT NULL,
    member_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    order_index       INTEGER NOT NULL CHECK(order_index >= 1),
    FOREIGN KEY (concept_id, short_code) REFERENCES variations(concept_id, short_code) ON DELETE CASCADE,
    PRIMARY KEY (concept_id, short_code, order_index)
);

CREATE TABLE IF NOT EXISTS aliases (
    alias       TEXT    PRIMARY KEY,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE
);

-- ── Tag 系统（记账元数据，编译器对这两张表全盲，不参与因果寻路）──
--
-- tags: tag 词表注册表。自然键（文本本身当主键），浏览时无需 JOIN 解码。
-- 词表是数据不是 schema：注册新 tag = 插一行（经 CLI create_tag，显式动作），
-- 但给概念盖未注册的 tag 会被外键当场拒绝——词汇漂移（plan/Plan/plans）在写入口就死。
-- 纪律：tag 的存在资格是有查询消费者，没有消费者的分类是装饰。
CREATE TABLE IF NOT EXISTS tags (
    name              TEXT PRIMARY KEY,
    source_concept_id INTEGER        -- NULL = system tag (plan, result)
);

-- 种子词表（当前有工作流消费者的 tag）：
--   'plan'   — 有序动作序列。消费者：audit_cluster('plan') 两条 lint
--              （未绑期待=不良构图；期待未结账=续接清单）。
--   'result' — 曾以结果身份出现的概念。消费者：goal 选单检索
--              （search_concepts --tag result）。
INSERT OR IGNORE INTO tags (name) VALUES ('plan'), ('result');

CREATE TABLE IF NOT EXISTS concept_tags (
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    tag         TEXT    NOT NULL REFERENCES tags(name) ON UPDATE CASCADE,
    PRIMARY KEY (concept_id, tag)
);

-- 跨 concept 名字唯一性在应用层校验：
-- 不同 concept 之间不能有任何名字重复（无论主名还是别名）。
-- 同一 concept 的主名可以同时出现在自己的 alias 中。

-- CLI 操作审计日志：谁在什么时候对哪个概念做了什么。
-- 不设 FK —— 概念删除后审计记录仍须保留。
CREATE TABLE IF NOT EXISTS cli_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    command      TEXT    NOT NULL,
    concept_id   INTEGER,
    concept_name TEXT,
    short_code   TEXT,
    sub_action   TEXT,
    success      INTEGER NOT NULL DEFAULT 1
);

-- ── Reminder 系统（轻量级提醒与收件箱，编译器全盲）──
--
-- 独立记录表，通过 FK 绑定到 concept。concept 删除时级联清理。
-- condition 存放 sandbox Python 表达式，由 inbox 命令 pull 求值。
CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    condition     TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_fired_at TEXT
);

-- 已发布数据库的增量升级记录。新数据库直接按本文件创建最新结构，
-- 并由 HoronDB 将随代码发布的迁移标记为已包含。
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_variations_concept ON variations(concept_id);
CREATE INDEX IF NOT EXISTS idx_cm_member           ON compose_members(member_concept_id);
CREATE INDEX IF NOT EXISTS idx_variations_type     ON variations(type);
CREATE INDEX IF NOT EXISTS idx_al_concept         ON aliases(concept_id);
CREATE INDEX IF NOT EXISTS idx_concept_tags_tag   ON concept_tags(tag);
CREATE INDEX IF NOT EXISTS idx_audit_concept      ON cli_audit_log(concept_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp    ON cli_audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_reminders_concept  ON reminders(concept_id);

-- GUI 可读视图
CREATE VIEW IF NOT EXISTS v_compose AS
SELECT
    cm.concept_id,
    cm.short_code,
    v.type   AS variation_type,
    c1.name  AS variation_concept_name,
    cm.member_concept_id,
    c2.name  AS member_concept_name,
    cm.order_index
FROM compose_members cm
JOIN concepts c1 ON cm.concept_id = c1.id
JOIN concepts c2 ON cm.member_concept_id = c2.id
JOIN variations v ON cm.concept_id = v.concept_id AND cm.short_code = v.short_code
ORDER BY cm.concept_id, cm.short_code, cm.order_index, cm.member_concept_id;

-- GUI 可读视图：浏览 tag 时直接看概念名，按 tag 归组
CREATE VIEW IF NOT EXISTS v_concept_tags AS
SELECT
    ct.concept_id,
    c.name AS concept_name,
    ct.tag
FROM concept_tags ct
JOIN concepts c ON ct.concept_id = c.id
ORDER BY ct.tag, c.name;
