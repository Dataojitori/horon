-- 007_pending_notifications.sql: 添加会话未读通知暂存表 (Pending Notifications Queue)

CREATE TABLE IF NOT EXISTS pending_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pn_session ON pending_notifications(session_id);
