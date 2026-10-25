-- The latest user message and the latest AI reply seen in each session. When a message
-- event goes to Jev, the other side's previous message is sent along as context.
ALTER TABLE sessions ADD COLUMN last_user_message TEXT;
ALTER TABLE sessions ADD COLUMN last_model_message TEXT;
