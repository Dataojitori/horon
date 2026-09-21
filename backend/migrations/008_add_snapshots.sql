-- 008_add_snapshots.sql: 添加 CLI 操作快照表 (Snapshots) 用于人工审核与回滚

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id      INTEGER NOT NULL,
    concept_name    TEXT    NOT NULL,
    field           TEXT    NOT NULL CHECK(field IN ('content', 'disclosure')),
    original_value  TEXT,                           -- 修改前的原始值（文本），若是新创建概念则为 NULL
    is_creation     INTEGER NOT NULL DEFAULT 0,     -- 是否为新建节点标记 (1=新建, 回滚时执行整节点删除)
    created_at      TEXT    NOT NULL,
    UNIQUE(concept_id, field)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_concept ON snapshots(concept_id);
