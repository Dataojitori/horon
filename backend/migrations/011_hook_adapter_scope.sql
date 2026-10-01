ALTER TABLE sensor_hooks ADD COLUMN adapter TEXT;
ALTER TABLE tool_guards ADD COLUMN adapter TEXT;

DROP INDEX IF EXISTS uidx_sh_event;
CREATE UNIQUE INDEX uidx_sh_event ON sensor_hooks(event_type, IFNULL(tool, ''), match_pattern, IFNULL(adapter, ''));
DROP INDEX IF EXISTS uidx_tg_rule;
CREATE UNIQUE INDEX uidx_tg_rule ON tool_guards(tool, IFNULL(args_pattern, ''), IFNULL(adapter, ''));
