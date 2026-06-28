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
    status      TEXT    CHECK(status IN ('hypothesis', 'confirmed', 'negated')),
    evidence    TEXT,
    unless      TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (concept_id, short_code)
);

CREATE TABLE IF NOT EXISTS compose_members (
    concept_id        INTEGER NOT NULL,
    short_code        TEXT    NOT NULL,
    member_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL CHECK(position IN (1, 2)),
    FOREIGN KEY (concept_id, short_code) REFERENCES variations(concept_id, short_code) ON DELETE CASCADE,
    PRIMARY KEY (concept_id, short_code, member_concept_id)
);

CREATE TABLE IF NOT EXISTS aliases (
    alias       TEXT    PRIMARY KEY,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE
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

CREATE INDEX IF NOT EXISTS idx_variations_concept ON variations(concept_id);
CREATE INDEX IF NOT EXISTS idx_cm_member          ON compose_members(member_concept_id);
CREATE INDEX IF NOT EXISTS idx_al_concept         ON aliases(concept_id);
CREATE INDEX IF NOT EXISTS idx_audit_concept      ON cli_audit_log(concept_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp    ON cli_audit_log(timestamp);

-- GUI 可读视图
CREATE VIEW IF NOT EXISTS v_compose AS
SELECT
    cm.concept_id,
    cm.short_code,
    c1.name  AS variation_concept_name,
    cm.member_concept_id,
    c2.name  AS member_concept_name,
    cm.position
FROM compose_members cm
JOIN concepts c1 ON cm.concept_id = c1.id
JOIN concepts c2 ON cm.member_concept_id = c2.id
ORDER BY cm.concept_id, cm.short_code, cm.position, cm.member_concept_id;
