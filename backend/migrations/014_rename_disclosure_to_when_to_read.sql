-- 书腰字段 disclosure 改名为 when_to_read：字段名直接说明该填什么（什么情况下打开这个节点），不留旧名。
ALTER TABLE concepts RENAME COLUMN disclosure TO when_to_read;

-- snapshots.field 的 CHECK 写死了 'disclosure'，SQLite 改不了 CHECK，只能重建表。
-- 只保留代码实际读写的列（旧库里残留的 session_id 列和 'deletion' 取值代码早已不用）。
CREATE TABLE snapshots_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id      INTEGER NOT NULL,
    concept_name    TEXT    NOT NULL,
    field           TEXT    NOT NULL CHECK(field IN ('content', 'when_to_read')),
    original_value  TEXT,
    is_creation     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    UNIQUE(concept_id, field)
);
INSERT INTO snapshots_new (id, concept_id, concept_name, field, original_value, is_creation, created_at)
SELECT id, concept_id, concept_name,
       CASE field WHEN 'disclosure' THEN 'when_to_read' ELSE field END,
       original_value, is_creation, created_at
FROM snapshots
WHERE field IN ('content', 'disclosure');
DROP TABLE snapshots;
ALTER TABLE snapshots_new RENAME TO snapshots;
CREATE INDEX IF NOT EXISTS idx_snapshots_concept ON snapshots(concept_id);
