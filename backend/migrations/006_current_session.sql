-- ============================================================
-- Migration 006: Current Active Session State Table
-- ============================================================

CREATE TABLE IF NOT EXISTS current_session (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
