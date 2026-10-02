-- A sensor hook may carry a yes/no question for Jev. The regex is the first-pass filter;
-- when it hits and jev_question is set, the sensor fires only if Jev's "yes" probability
-- is >= jev_threshold. NULL jev_question keeps the regex-only behaviour.
ALTER TABLE sensor_hooks ADD COLUMN jev_question TEXT;
ALTER TABLE sensor_hooks ADD COLUMN jev_threshold REAL;
