-- Agent Team: optional server/Turso database context settings per project.
-- Prerequisite: the existing project table must be present.
-- Run only if PRAGMA table_info(project) does not include database_connections.
-- The application may already have added this column automatically.
-- This ALTER TABLE is a one-time migration; do not rerun it.
-- Values are JSON connection settings with secret environment variable names.
-- Do not store actual passwords or tokens in this column.

BEGIN TRANSACTION;

ALTER TABLE project
    ADD COLUMN database_connections TEXT NOT NULL DEFAULT '[]';

COMMIT;
