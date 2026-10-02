-- Agent Team: repository context snapshots, artifacts, and relationship graph.
-- Apply to Agent Team's Turso storage database (TURSO_URL).
-- Matches TursoStore._ensure_schema. Existing records are preserved.
-- This migration is safe to rerun.

BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS repo_map_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    repository TEXT NOT NULL,
    revision TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL,
    artifact_count INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_repo_map_snapshot_repository
    ON repo_map_snapshot(repository, revision, is_current);

CREATE INDEX IF NOT EXISTS idx_repo_map_snapshot_project
    ON repo_map_snapshot(project_id, is_current, updated_at);

CREATE TABLE IF NOT EXISTS repo_map_artifact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_id, artifact_path)
);

CREATE INDEX IF NOT EXISTS idx_repo_map_artifact_snapshot
    ON repo_map_artifact(snapshot_id, artifact_path);

CREATE TABLE IF NOT EXISTS repo_relationship (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    target_path TEXT NOT NULL DEFAULT '',
    target_name TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_repo_relationship_snapshot
    ON repo_relationship(snapshot_id, relation_type);

COMMIT;
