ALTER TABLE concepts ADD COLUMN scope_audit_fingerprint TEXT;

CREATE TABLE IF NOT EXISTS concept_name_embeddings (
    alias           TEXT    PRIMARY KEY REFERENCES aliases(alias) ON DELETE CASCADE,
    embedding       BLOB    NOT NULL,
    embedding_model TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
