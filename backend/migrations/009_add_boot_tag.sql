-- 系统保留 Tag `boot`：挂上它的节点会在 `cli.py login` 时全文输出（会话启动必读清单）。
-- 同时登记在 _db_common.SYSTEM_TAGS 与 schema.sql 种子行中；此处保证旧库留下迁移记录。
INSERT OR IGNORE INTO tags (name) VALUES ('boot');
