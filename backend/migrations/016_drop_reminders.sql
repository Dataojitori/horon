-- The reminder / inbox feature was removed: Horon detects events through sensors and guards,
-- and no longer stores condition rules that something has to remember to poll.
DROP INDEX IF EXISTS idx_reminders_concept;
DROP TABLE IF EXISTS reminders;
