-- Timestamps used to be written in the local time of the machine that wrote them, with no time zone,
-- so the same database could hold times from different zones that looked alike.
-- From now on every timestamp is UTC with an explicit offset: 2026-11-03T06:55:01+00:00.
-- This converts the old values. They are read as the local time of the machine running this
-- migration (SQLite's 'utc' modifier, which also applies daylight saving time). A database that
-- was written in another time zone has to be converted by hand before this migration runs on it.
-- Only values in the old format are touched, so values that already carry an offset stay as they are.

UPDATE concepts SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE concepts SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at, 'utc')
    WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE sensor_hooks SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE tool_guards SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE inhibitions SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE cli_audit_log SET timestamp = strftime('%Y-%m-%dT%H:%M:%S+00:00', timestamp, 'utc')
    WHERE timestamp GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
-- seed_transitions.py marks edges that were never really walked with this fixed value; keep it fixed, not shifted.
UPDATE concept_transitions SET last_accessed_at = '1970-01-01T00:00:00+00:00'
    WHERE last_accessed_at = '1970-01-01T00:00:00';
UPDATE concept_transitions SET last_accessed_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', last_accessed_at, 'utc')
    WHERE last_accessed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE schema_migrations SET applied_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', applied_at, 'utc')
    WHERE applied_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE sessions SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE sessions SET last_active_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', last_active_at, 'utc')
    WHERE last_active_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE session_active_sensors SET activated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', activated_at, 'utc')
    WHERE activated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE concept_embeddings SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at, 'utc')
    WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE concept_name_embeddings SET updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', updated_at, 'utc')
    WHERE updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE pending_notifications SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
UPDATE snapshots SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', created_at, 'utc')
    WHERE created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]';
