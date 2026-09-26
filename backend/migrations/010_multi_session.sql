-- Migration 010: Multi-session support (per-session sensor states)
--
-- 1. 新增 sessions 表记录所有已知会话、活跃时间与来自哪个宿主
-- 2. 新增 session_active_sensors 表，按会话记录已激活的 turn/session 传感器
-- 3. 彻底移除单例 current_session 表
-- 4. 插入默认离线开发会话 devonly

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL,
    adapter         TEXT             -- 来自哪个宿主（claude-code / codex / antigravity），只供网页显示
);

-- 每个会话里当前处于激活状态的 turn/session 传感器：有这一行 = 激活，熄灭即删除。
-- 永久传感器存在 concepts.is_active；逻辑节点和守卫不存，读取时按规则现算。
CREATE TABLE IF NOT EXISTS session_active_sensors (
    session_id      TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    activated_at    TEXT    NOT NULL,
    PRIMARY KEY (session_id, concept_id)
);

DROP TABLE IF EXISTS current_session;

INSERT OR IGNORE INTO sessions (session_id, created_at, last_active_at)
VALUES ('devonly', strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'), strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'));
