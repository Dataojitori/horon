-- Multi-Disclosure: 书腰从 concepts.disclosure 单字段迁移到独立表
CREATE TABLE IF NOT EXISTS disclosures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id  INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    text        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_disclosures_concept ON disclosures(concept_id);

-- 迁移旧数据（concepts.disclosure 列保留不删，代码不再读写它）
INSERT INTO disclosures (concept_id, text, created_at)
SELECT c.id, c.disclosure, c.updated_at FROM concepts c
WHERE c.disclosure IS NOT NULL AND c.disclosure != ''
  AND NOT EXISTS (
    SELECT 1 FROM disclosures d WHERE d.concept_id = c.id
  );

-- Attention Routing: 概念间的注意力转移突触权重
CREATE TABLE IF NOT EXISTS concept_transitions (
    from_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    to_concept_id   INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    weight          REAL    NOT NULL DEFAULT 0.0,
    last_accessed_at TEXT   NOT NULL,
    PRIMARY KEY (from_concept_id, to_concept_id)
);
