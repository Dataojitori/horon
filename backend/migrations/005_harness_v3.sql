-- ============================================================
-- Migration 005: Horon v3 Harness Architecture Upgrade
--
-- 1. Abolihes variations table; flat concepts with role, lifespan, activation_type
-- 2. Refactors compose_members to (parent_concept_id, member_concept_id, order_index)
-- 3. Adds sensor_hooks, tool_guards, inhibitions, active_chain_instances
-- 4. Refactors cli_audit_log to include session_id and drop short_code
-- ============================================================

-- Drop old views that depend on variations / compose_members
DROP VIEW IF EXISTS v_compose;
DROP VIEW IF EXISTS v_concept_tags;

-- Build a stable mapping before flattening.  The lexicographically first
-- variation keeps the legacy concept id/name; every additional variation is
-- materialized as its own concept so no content or topology is discarded.
CREATE TABLE _variation_map (
    legacy_concept_id INTEGER NOT NULL,
    short_code        TEXT    NOT NULL,
    target_concept_id INTEGER NOT NULL UNIQUE,
    is_primary        INTEGER NOT NULL CHECK(is_primary IN (0, 1)),
    PRIMARY KEY (legacy_concept_id, short_code)
);

INSERT INTO _variation_map
    (legacy_concept_id, short_code, target_concept_id, is_primary)
SELECT v.concept_id, v.short_code, v.concept_id, 1
FROM variations v
WHERE v.short_code = (
    SELECT MIN(v2.short_code)
    FROM variations v2
    WHERE v2.concept_id = v.concept_id
);

INSERT INTO _variation_map
    (legacy_concept_id, short_code, target_concept_id, is_primary)
SELECT
    v.concept_id,
    v.short_code,
    (SELECT COALESCE(MAX(id), 0) FROM concepts)
        + ROW_NUMBER() OVER (ORDER BY v.concept_id, v.short_code),
    0
FROM variations v
WHERE v.short_code != (
    SELECT MIN(v2.short_code)
    FROM variations v2
    WHERE v2.concept_id = v.concept_id
);

-- 1. Create concepts_new with v3 structure and CHECK constraint
CREATE TABLE concepts_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    content         TEXT,
    disclosure      TEXT,
    role            TEXT    NOT NULL DEFAULT 'plain'
                            CHECK(role IN ('plain', 'sensor', 'logic', 'guard')),
    is_active       INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),     
    lifespan        TEXT    CHECK(lifespan IS NULL OR lifespan IN ('turn', 'session', 'permanent')),
    activation_type TEXT    CHECK(activation_type IS NULL OR activation_type IN ('CHAIN', 'AND', 'OR')),
    on_fire         TEXT,                           
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (
        (role = 'plain'  AND activation_type IS NULL     AND lifespan IS NULL     AND is_active = 0) OR
        (role = 'sensor' AND activation_type IS NULL     AND lifespan IS NOT NULL) OR
        (role = 'logic'  AND activation_type IS NOT NULL AND lifespan IS NULL) OR
        (role = 'guard'  AND activation_type IS NOT NULL AND lifespan IS NULL)
    )
);

-- Copy each legacy concept using its primary variation.  A concept without a
-- variation is retained as an empty plain concept for defensive compatibility.
INSERT INTO concepts_new (id, name, content, disclosure, role, is_active, lifespan, activation_type, on_fire, created_at, updated_at)
SELECT
    c.id,
    c.name,
    v.content,
    (SELECT d.text FROM disclosures d WHERE d.concept_id = c.id ORDER BY d.id LIMIT 1),
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = c.id AND cm.short_code = pm.short_code
        ) THEN 'logic'
        WHEN EXISTS (SELECT 1 FROM concept_tags ct WHERE ct.concept_id = c.id AND ct.tag IN ('state', 'action', 'result'))
         AND EXISTS (SELECT 1 FROM compose_members incoming WHERE incoming.member_concept_id = c.id) THEN 'sensor'
        ELSE 'plain'
    END AS role,
    0 AS is_active,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = c.id AND cm.short_code = pm.short_code
        ) THEN NULL
        WHEN EXISTS (SELECT 1 FROM concept_tags ct WHERE ct.concept_id = c.id AND ct.tag IN ('state', 'action', 'result'))
         AND EXISTS (SELECT 1 FROM compose_members incoming WHERE incoming.member_concept_id = c.id) THEN 'session'
        ELSE NULL
    END AS lifespan,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = c.id AND cm.short_code = pm.short_code
        ) THEN COALESCE(v.type, 'AND')
        ELSE NULL
    END AS activation_type,
    NULL AS on_fire,
    c.created_at,
    c.updated_at
FROM concepts c
LEFT JOIN _variation_map pm
  ON pm.legacy_concept_id = c.id AND pm.is_primary = 1
LEFT JOIN variations v
  ON v.concept_id = pm.legacy_concept_id AND v.short_code = pm.short_code;

-- Materialize every non-primary variation.  The generated name contains both
-- the legacy concept id and short code; a collision aborts the transaction
-- instead of silently overwriting or dropping user data.
INSERT INTO concepts_new (id, name, content, disclosure, role, is_active, lifespan, activation_type, on_fire, created_at, updated_at)
SELECT
    vm.target_concept_id,
    c.name || ' [legacy variation ' || c.id || '-' || v.short_code || ']',
    v.content,
    NULL,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = v.concept_id AND cm.short_code = v.short_code
        ) THEN 'logic'
        WHEN EXISTS (SELECT 1 FROM concept_tags ct WHERE ct.concept_id = c.id AND ct.tag IN ('state', 'action', 'result'))
         AND EXISTS (SELECT 1 FROM compose_members incoming WHERE incoming.member_concept_id = c.id) THEN 'sensor'
        ELSE 'plain'
    END,
    0,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = v.concept_id AND cm.short_code = v.short_code
        ) THEN NULL
        WHEN EXISTS (SELECT 1 FROM concept_tags ct WHERE ct.concept_id = c.id AND ct.tag IN ('state', 'action', 'result'))
         AND EXISTS (SELECT 1 FROM compose_members incoming WHERE incoming.member_concept_id = c.id) THEN 'session'
        ELSE NULL
    END,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM compose_members cm
            WHERE cm.concept_id = v.concept_id AND cm.short_code = v.short_code
        ) THEN COALESCE(v.type, 'AND')
        ELSE NULL
    END,
    NULL,
    v.created_at,
    v.updated_at
FROM _variation_map vm
JOIN variations v
  ON v.concept_id = vm.legacy_concept_id AND v.short_code = vm.short_code
JOIN concepts c ON c.id = vm.legacy_concept_id
WHERE vm.is_primary = 0;

-- 2. Stage compose_members data
CREATE TABLE _cm_data (
    parent_concept_id INTEGER NOT NULL,
    member_concept_id INTEGER NOT NULL,
    order_index       INTEGER NOT NULL
);

INSERT INTO _cm_data (parent_concept_id, member_concept_id, order_index)
SELECT vm.target_concept_id, cm.member_concept_id, cm.order_index
FROM compose_members cm
JOIN _variation_map vm
  ON vm.legacy_concept_id = cm.concept_id AND vm.short_code = cm.short_code;

-- 3. Create cli_audit_log_new with session_id
CREATE TABLE cli_audit_log_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL DEFAULT 'default',
    timestamp    TEXT    NOT NULL,
    command      TEXT    NOT NULL,
    concept_id   INTEGER,
    concept_name TEXT,
    sub_action   TEXT,
    success      INTEGER NOT NULL DEFAULT 1
);

INSERT INTO cli_audit_log_new (id, session_id, timestamp, command, concept_id, concept_name, sub_action, success)
SELECT id, 'default', timestamp, command, concept_id, concept_name, sub_action, success
FROM cli_audit_log;

-- 4. Drop legacy tables and swap main entities
DROP TABLE compose_members;
DROP TABLE cli_audit_log;
ALTER TABLE cli_audit_log_new RENAME TO cli_audit_log;

DROP TABLE concepts;
ALTER TABLE concepts_new RENAME TO concepts;

CREATE TABLE IF NOT EXISTS concept_embeddings (
    concept_id      INTEGER PRIMARY KEY REFERENCES concepts(id) ON DELETE CASCADE,
    embedding       BLOB    NOT NULL,
    embedding_model TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

INSERT INTO concept_embeddings (concept_id, embedding, embedding_model, updated_at)
SELECT
    d.concept_id,
    d.embedding,
    d.embedding_model,
    d.created_at
FROM disclosures d
WHERE d.embedding IS NOT NULL
  AND d.id = (
      SELECT MIN(d2.id)
      FROM disclosures d2
      WHERE d2.concept_id = d.concept_id AND d2.embedding IS NOT NULL
  );

DROP TABLE IF EXISTS variations;
DROP TABLE IF EXISTS disclosures;

-- Give materialized alternatives a resolvable name through the same alias
-- authority used by normal concept creation.
INSERT INTO aliases (alias, concept_id)
SELECT c.name, c.id
FROM concepts c
JOIN _variation_map vm ON vm.target_concept_id = c.id
WHERE vm.is_primary = 0;

-- Tags were concept-level metadata in v2, so every concept materialized from
-- one of that concept's variations must remain in the same tag clusters.
INSERT INTO concept_tags (concept_id, tag)
SELECT vm.target_concept_id, ct.tag
FROM _variation_map vm
JOIN concept_tags ct ON ct.concept_id = vm.legacy_concept_id
WHERE vm.is_primary = 0;

-- 5. Create new Harness tables referencing concepts(id)
CREATE TABLE compose_members (
    parent_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    member_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    order_index       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (parent_concept_id, order_index)
);

INSERT INTO compose_members (parent_concept_id, member_concept_id, order_index)
SELECT parent_concept_id, member_concept_id, order_index FROM _cm_data;

DROP TABLE _cm_data;
DROP TABLE _variation_map;

CREATE TABLE IF NOT EXISTS sensor_hooks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_concept_id INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE,
    event_type        TEXT    NOT NULL
                      CHECK(event_type IN ('user_message', 'model_message', 'tool_call', 'tool_result')),
    tool              TEXT,
    match_pattern     TEXT    NOT NULL,
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_guards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guard_concept_id  INTEGER NOT NULL UNIQUE REFERENCES concepts(id) ON DELETE CASCADE,
    tool              TEXT    NOT NULL,
    args_pattern      TEXT,
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS inhibitions (
    target_concept_id    INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    inhibitor_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    created_at           TEXT    NOT NULL,
    PRIMARY KEY (target_concept_id, inhibitor_concept_id)
);

CREATE TABLE IF NOT EXISTS active_chain_instances (
    chain_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    current_order    INTEGER NOT NULL DEFAULT 0,
    session_id       TEXT    NOT NULL,
    PRIMARY KEY (chain_concept_id, current_order, session_id)
);

-- 6. Create Indexes
CREATE INDEX IF NOT EXISTS idx_cm_member           ON compose_members(member_concept_id);
CREATE INDEX IF NOT EXISTS idx_sh_lookup           ON sensor_hooks(event_type, tool);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_sh_event   ON sensor_hooks(event_type, IFNULL(tool, ''), match_pattern);
CREATE INDEX IF NOT EXISTS idx_tg_tool            ON tool_guards(tool);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_tg_rule    ON tool_guards(tool, IFNULL(args_pattern, ''));
CREATE INDEX IF NOT EXISTS idx_inh_inhibitor      ON inhibitions(inhibitor_concept_id);
CREATE INDEX IF NOT EXISTS idx_aci_session        ON active_chain_instances(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_session      ON cli_audit_log(session_id, timestamp);

-- 7. Recreate Views
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
