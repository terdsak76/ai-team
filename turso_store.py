"""Persistence for agent outputs in Turso."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv
from muse.repository_context import RepositoryContext
from muse.database_context import validate_database_connections

load_dotenv()


class TursoConfigurationError(RuntimeError):
    """Raised when the Turso connection is not configured or available."""


class TursoStore:
    """Stores one current output row per agent for each workflow run."""

    PROJECT_PROMPT_KEYS = ("specification", "ui_ux", "frontend", "backend", "tester")

    PROJECT_PROMPT_COLUMNS = {
        "specification": "system_prompt_specification",
        "ui_ux": "system_prompt_ui_ux",
        "frontend": "system_prompt_frontend",
        "backend": "system_prompt_backend",
        "tester": "system_prompt_tester",
    }

    def __init__(self, url: str | None = None, token: str | None = None):
        self.url = url or os.getenv("TURSO_URL")
        self.token = token or os.getenv("TURSO_TOKEN")

    def _connect(self):
        if not self.url or not self.token:
            raise TursoConfigurationError("TURSO_URL and TURSO_TOKEN must be configured.")
        try:
            import turso_serverless
        except ImportError as error:
            raise TursoConfigurationError(
                "The turso_serverless package is not installed. Install project dependencies first."
            ) from error
        return turso_serverless.connect(self.url, auth_token=self.token)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _serialize(value: Any) -> str:
        if hasattr(value, "model_dump_json"):
            return value.model_dump_json(indent=2)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _ensure_schema(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                github_repo TEXT NOT NULL DEFAULT '',
                github_token TEXT NOT NULL DEFAULT '',
                project_context TEXT NOT NULL DEFAULT '',
                system_prompt_specification TEXT NOT NULL,
                system_prompt_ui_ux TEXT NOT NULL,
                system_prompt_frontend TEXT NOT NULL,
                system_prompt_backend TEXT NOT NULL,
                system_prompt_tester TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_name ON project(project_name)"
        )
        project_columns = conn.execute("PRAGMA table_info(project)").fetchall()
        if "project_context" not in {row[1] for row in project_columns}:
            conn.execute(
                "ALTER TABLE project ADD COLUMN project_context TEXT NOT NULL DEFAULT ''"
            )
        if "database_connections" not in {row[1] for row in project_columns}:
            conn.execute("ALTER TABLE project ADD COLUMN database_connections TEXT NOT NULL DEFAULT '[]'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                memory_type TEXT NOT NULL DEFAULT 'task_summary',
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                source_run_id TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_memory_project "
            "ON project_memory(project_id, is_active, updated_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_output (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                requirement_code TEXT NOT NULL,
                project_name TEXT NOT NULL,
                request_text TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                output TEXT NOT NULL,
                system_prompt TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                is_current INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        prompt_output_columns = conn.execute("PRAGMA table_info(prompt_output)").fetchall()
        if "system_prompt" not in {row[1] for row in prompt_output_columns}:
            conn.execute(
                "ALTER TABLE prompt_output ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_output_requirement_code "
            "ON prompt_output(requirement_code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_output_project_name "
            "ON prompt_output(project_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_output_run_id "
            "ON prompt_output(run_id)"
        )
        conn.execute(
            """
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
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repo_map_snapshot_repository "
            "ON repo_map_snapshot(repository, revision, is_current)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repo_map_snapshot_project "
            "ON repo_map_snapshot(project_id, is_current, updated_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repo_map_artifact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(snapshot_id, artifact_path)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repo_map_artifact_snapshot "
            "ON repo_map_artifact(snapshot_id, artifact_path)"
        )
        conn.execute(
            """
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
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repo_relationship_snapshot "
            "ON repo_relationship(snapshot_id, relation_type)"
        )

    @classmethod
    def _project_from_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        prompts = {
            key: row[cls.PROJECT_PROMPT_COLUMNS[key]]
            for key in cls.PROJECT_PROMPT_KEYS
        }
        return {
            "id": int(row["id"]),
            "project_name": row["project_name"],
            "github_repo": row["github_repo"] or "",
            "github_token": row.get("github_token", "") or "",
            "github_token_set": bool(row.get("github_token", "")),
            "project_context": row.get("project_context", "") or "",
            "database_connections": json.loads(row.get("database_connections") or "[]"),
            "system_prompts": prompts,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _project_params(
        cls,
        *,
        project_name: str,
        github_repo: str,
        github_token: str,
        project_context: str,
        system_prompts: dict[str, str],
        timestamp: str,
    ) -> tuple[Any, ...]:
        return (
            project_name,
            github_repo,
            github_token,
            project_context,
            *(system_prompts[key] for key in cls.PROJECT_PROMPT_KEYS),
            timestamp,
            timestamp,
        )

    def list_projects(self) -> list[dict[str, Any]]:
        def query(conn):
            cursor = conn.execute(
                "SELECT id, project_name, github_repo, github_token, project_context, database_connections, "
                "system_prompt_specification, system_prompt_ui_ux, "
                "system_prompt_frontend, system_prompt_backend, system_prompt_tester, "
                "created_at, updated_at FROM project ORDER BY project_name COLLATE NOCASE"
            )
            projects = []
            for row in self._rows(cursor):
                project = self._project_from_row(row)
                project.pop("github_token", None)
                projects.append(project)
            return projects

        return self._with_connection(query)

    def get_project(self, project_id: int) -> dict[str, Any]:
        def query(conn):
            cursor = conn.execute(
                "SELECT id, project_name, github_repo, github_token, project_context, database_connections, "
                "system_prompt_specification, system_prompt_ui_ux, "
                "system_prompt_frontend, system_prompt_backend, system_prompt_tester, "
                "created_at, updated_at FROM project WHERE id = ?",
                (project_id,),
            )
            rows = self._rows(cursor)
            if not rows:
                raise LookupError("The selected project was not found.")
            return self._project_from_row(rows[0])

        return self._with_connection(query)

    def create_project(
        self,
        *,
        project_name: str,
        github_repo: str = "",
        github_token: str = "",
        project_context: str = "",
        system_prompts: dict[str, str],
        database_connections: list[dict] | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        connections = validate_database_connections(database_connections)

        def insert(conn):
            cursor = conn.execute(
                """
                INSERT INTO project (
                    project_name, github_repo, github_token, project_context,
                    system_prompt_specification, system_prompt_ui_ux,
                    system_prompt_frontend, system_prompt_backend, system_prompt_tester,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._project_params(
                    project_name=project_name,
                    github_repo=github_repo,
                    github_token=github_token,
                    project_context=project_context,
                    system_prompts=system_prompts,
                    timestamp=timestamp,
                ),
            )
            project_id = int(cursor.lastrowid)
            conn.execute("UPDATE project SET database_connections = ? WHERE id = ?",
                         (json.dumps(connections), project_id))
            return project_id

        project_id = self._with_connection(insert)
        return self.get_project(project_id)

    def update_project(
        self,
        *,
        project_id: int,
        project_name: str,
        github_repo: str = "",
        github_token: str | None = None,
        project_context: str = "",
        clear_github_token: bool = False,
        system_prompts: dict[str, str],
        database_connections: list[dict] | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        connections = validate_database_connections(database_connections) if database_connections is not None else None

        def update(conn):
            current_cursor = conn.execute(
                "SELECT github_token FROM project WHERE id = ?",
                (project_id,),
            )
            current_rows = self._rows(current_cursor)
            if not current_rows:
                raise LookupError("The selected project was not found.")
            current_token = current_rows[0]["github_token"] or ""
            saved_token = "" if clear_github_token else (github_token or current_token)
            cursor = conn.execute(
                """
                UPDATE project SET
                    project_name = ?, github_repo = ?, github_token = ?, project_context = ?,
                    system_prompt_specification = ?, system_prompt_ui_ux = ?,
                    system_prompt_frontend = ?, system_prompt_backend = ?,
                    system_prompt_tester = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    project_name,
                    github_repo,
                    saved_token,
                    project_context,
                    *(system_prompts[key] for key in self.PROJECT_PROMPT_KEYS),
                    timestamp,
                    project_id,
                ),
            )
            if cursor.rowcount == 0:
                raise LookupError("The selected project was not found.")
            if connections is not None:
                conn.execute("UPDATE project SET database_connections = ? WHERE id = ?",
                             (json.dumps(connections), project_id))

        self._with_connection(update)
        return self.get_project(project_id)

    @staticmethod
    def _memory_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "project_id": int(row["project_id"]),
            "memory_type": row["memory_type"],
            "content": row["content"],
            "tags": row["tags"] or "",
            "source_run_id": row.get("source_run_id"),
            "confidence": float(row["confidence"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_project_memory(
        self,
        *,
        project_id: int,
        content: str,
        memory_type: str = "task_summary",
        tags: str = "",
        source_run_id: str | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        if not content or not content.strip():
            raise ValueError("Project memory cannot be empty.")
        timestamp = self._now()

        def insert(conn):
            cursor = conn.execute(
                """
                INSERT INTO project_memory (
                    project_id, memory_type, content, tags, source_run_id,
                    confidence, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    project_id,
                    memory_type.strip() or "task_summary",
                    content.strip()[:12_000],
                    tags.strip()[:500],
                    source_run_id,
                    max(0.0, min(1.0, float(confidence))),
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

        memory_id = self._with_connection(insert)

        def query(conn):
            cursor = conn.execute(
                "SELECT id, project_id, memory_type, content, tags, source_run_id, "
                "confidence, created_at, updated_at FROM project_memory WHERE id = ?",
                (memory_id,),
            )
            rows = self._rows(cursor)
            if not rows:
                raise LookupError("The project memory was not found after saving.")
            return self._memory_from_row(rows[0])

        return self._with_connection(query)

    def list_project_memories(
        self, *, project_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        def query(conn):
            cursor = conn.execute(
                "SELECT id, project_id, memory_type, content, tags, source_run_id, "
                "confidence, created_at, updated_at FROM project_memory "
                "WHERE project_id = ? AND is_active = 1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (project_id, max(1, min(200, limit))),
            )
            return [self._memory_from_row(row) for row in self._rows(cursor)]

        return self._with_connection(query)

    def get_relevant_project_memories(
        self, *, project_id: int, task_text: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        stop_words = {
            "the", "and", "for", "with", "from", "that", "this", "will",
            "should", "could", "would", "into", "have", "has", "are", "was",
            "were", "its", "our", "their", "new", "task", "build", "create",
        }
        terms = {
            term
            for term in re.findall(r"[a-z0-9_]{3,}", (task_text or "").casefold())
            if term not in stop_words
        }

        def query(conn):
            cursor = conn.execute(
                "SELECT id, project_id, memory_type, content, tags, source_run_id, "
                "confidence, created_at, updated_at FROM project_memory "
                "WHERE project_id = ? AND is_active = 1 "
                "ORDER BY updated_at DESC LIMIT 200",
                (project_id,),
            )
            ranked = []
            for row in self._rows(cursor):
                memory = self._memory_from_row(row)
                haystack = f"{memory['tags']} {memory['content']}".casefold()
                score = sum(1 for term in terms if term in haystack)
                if score:
                    ranked.append((score, memory["updated_at"], memory))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return [item[2] for item in ranked[: max(1, min(20, limit))]]

        return self._with_connection(query)

    def save_repository_context(
        self,
        *,
        context: RepositoryContext,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist a versioned `.repo-map` snapshot and its relationship graph."""

        if not context.repository:
            raise ValueError("A repository name is required to persist repository context.")
        artifacts = context.artifacts()
        timestamp = self._now()

        def insert(conn):
            if project_id is None:
                conn.execute(
                    "UPDATE repo_map_snapshot SET is_current = 0, updated_at = ? "
                    "WHERE project_id IS NULL AND repository = ? AND is_current = 1",
                    (timestamp, context.repository),
                )
            else:
                conn.execute(
                    "UPDATE repo_map_snapshot SET is_current = 0, updated_at = ? "
                    "WHERE project_id = ? AND repository = ? AND is_current = 1",
                    (timestamp, project_id, context.repository),
                )
            cursor = conn.execute(
                """
                INSERT INTO repo_map_snapshot (
                    project_id, repository, revision, context_json, artifact_count,
                    is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    project_id,
                    context.repository,
                    context.revision,
                    self._serialize(context.as_dict()),
                    len(artifacts),
                    timestamp,
                    timestamp,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for artifact_path, content in artifacts.items():
                if artifact_path.endswith(".md"):
                    artifact_type = "markdown"
                elif artifact_path.endswith(".patch"):
                    artifact_type = "patch"
                else:
                    artifact_type = "json"
                conn.execute(
                    """
                    INSERT INTO repo_map_artifact (
                        snapshot_id, artifact_path, artifact_type, content,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        artifact_path,
                        artifact_type,
                        content,
                        hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        timestamp,
                    ),
                )
            for relationship in context.relationships:
                conn.execute(
                    """
                    INSERT INTO repo_relationship (
                        snapshot_id, source_path, source_name, source_kind,
                        target_path, target_name, target_kind, relation_type,
                        confidence, evidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        relationship.source_path,
                        relationship.source_name,
                        relationship.source_kind,
                        relationship.target_path,
                        relationship.target_name,
                        relationship.target_kind,
                        relationship.relation_type,
                        max(0.0, min(1.0, float(relationship.confidence))),
                        relationship.evidence,
                        timestamp,
                    ),
                )
            return snapshot_id

        snapshot_id = self._with_connection(insert)
        return {
            "snapshot_id": snapshot_id,
            "project_id": project_id,
            "repository": context.repository,
            "revision": context.revision,
            "artifact_count": len(artifacts),
            "relationship_count": len(context.relationships),
            "created_at": timestamp,
        }

    def get_current_repository_context(self, *, repository: str, project_id: int | None = None) -> dict | None:
        """Read the current snapshot metadata/index only, not all artifact bodies."""
        def query(conn):
            scope = "project_id IS NULL" if project_id is None else "project_id = ?"
            params = (repository,) if project_id is None else (repository, project_id)
            rows = self._rows(conn.execute(
                "SELECT id, project_id, repository, revision, context_json, artifact_count, created_at "
                f"FROM repo_map_snapshot WHERE repository = ? AND {scope} AND is_current = 1 "
                "ORDER BY id DESC LIMIT 1", params,
            ))
            return rows[0] if rows else None
        return self._with_connection(query)

    def get_repository_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """Load one persisted repo-map snapshot, including artifacts and edges."""

        def query(conn):
            snapshot_rows = self._rows(
                conn.execute(
                    "SELECT id, project_id, repository, revision, context_json, "
                    "artifact_count, is_current, created_at, updated_at "
                    "FROM repo_map_snapshot WHERE id = ?",
                    (snapshot_id,),
                )
            )
            if not snapshot_rows:
                raise LookupError("The repository snapshot was not found.")
            snapshot = snapshot_rows[0]
            artifacts = self._rows(
                conn.execute(
                    "SELECT artifact_path, artifact_type, content, content_sha256, created_at "
                    "FROM repo_map_artifact WHERE snapshot_id = ? ORDER BY artifact_path",
                    (snapshot_id,),
                )
            )
            relationships = self._rows(
                conn.execute(
                    "SELECT source_path, source_name, source_kind, target_path, target_name, "
                    "target_kind, relation_type, confidence, evidence, created_at "
                    "FROM repo_relationship WHERE snapshot_id = ? "
                    "ORDER BY source_path, source_name, target_name",
                    (snapshot_id,),
                )
            )
            snapshot["artifacts"] = artifacts
            snapshot["relationships"] = relationships
            return snapshot

        return self._with_connection(query)

    def _with_connection(self, operation: Callable[[Any], Any]) -> Any:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            result = operation(conn)
            conn.commit()
            return result
        finally:
            conn.close()

    @staticmethod
    def _rows(cursor) -> list[dict[str, Any]]:
        columns = [description[0] for description in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        runs: dict[str, dict[str, Any]] = {}
        for row in rows:
            run = runs.setdefault(
                row["run_id"],
                {
                    "run_id": row["run_id"],
                    "requirement_code": row["requirement_code"],
                    "project_name": row["project_name"],
                    "request_text": row["request_text"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "outputs": {},
                    "prompts": {},
                },
            )
            run["outputs"][row["agent_name"]] = row["output"]
            run["prompts"][row["agent_name"]] = row.get("system_prompt", "")
            run["updated_at"] = max(run["updated_at"], row["updated_at"])
        return list(runs.values())

    def save_run(
        self,
        *,
        requirement_code: str,
        project_name: str,
        request_text: str,
        outputs: dict[str, Any],
        system_prompts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        timestamp = self._now()

        def insert(conn):
            for agent_name, output in outputs.items():
                conn.execute(
                    """
                    INSERT INTO prompt_output (
                        run_id, requirement_code, project_name, request_text,
                        agent_name, output, system_prompt, version, is_current, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        run_id,
                        requirement_code,
                        project_name,
                        request_text,
                        agent_name,
                        self._serialize(output),
                        (system_prompts or {}).get(agent_name, ""),
                        timestamp,
                        timestamp,
                    ),
                )

        self._with_connection(insert)
        return {
            "run_id": run_id,
            "requirement_code": requirement_code,
            "project_name": project_name,
        }

    def query_runs(
        self,
        *,
        requirement_code: str | None = None,
        project_name: str | None = None,
    ) -> list[dict[str, Any]]:
        if not requirement_code and not project_name:
            raise ValueError("Provide a requirement code or project name.")

        def query(conn):
            clauses = ["is_current = 1"]
            params: list[str] = []
            if requirement_code:
                clauses.append("LOWER(requirement_code) = LOWER(?)")
                params.append(requirement_code)
            if project_name:
                clauses.append("LOWER(project_name) LIKE LOWER(?)")
                params.append(f"%{project_name}%")
            cursor = conn.execute(
                "SELECT run_id, requirement_code, project_name, request_text, "
                "agent_name, output, version, created_at, updated_at, system_prompt "
                "FROM prompt_output WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC",
                params,
            )
            return self._group_rows(self._rows(cursor))

        return self._with_connection(query)

    def get_run(self, run_id: str) -> dict[str, Any]:
        def query(conn):
            cursor = conn.execute(
                "SELECT run_id, requirement_code, project_name, request_text, "
                "agent_name, output, version, created_at, updated_at, system_prompt "
                "FROM prompt_output WHERE run_id = ? AND is_current = 1 "
                "ORDER BY created_at DESC, id DESC",
                (run_id,),
            )
            runs = self._group_rows(self._rows(cursor))
            if not runs:
                raise LookupError("No saved outputs were found for that run.")
            return runs[0]

        return self._with_connection(query)

    def update_specification(self, *, run_id: str, specification: str) -> dict[str, Any]:
        if not specification.strip():
            raise ValueError("The specification cannot be empty.")

        def update(conn):
            cursor = conn.execute(
                "SELECT requirement_code, project_name, request_text, system_prompt, version "
                "FROM prompt_output "
                "WHERE run_id = ? AND agent_name = 'specification' AND is_current = 1 "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            )
            rows = self._rows(cursor)
            if not rows:
                raise LookupError("No saved specification was found for that run.")
            current = rows[0]
            timestamp = self._now()
            conn.execute(
                "UPDATE prompt_output SET is_current = 0, updated_at = ? "
                "WHERE run_id = ? AND agent_name = 'specification' AND is_current = 1",
                (timestamp, run_id),
            )
            conn.execute(
                """
                INSERT INTO prompt_output (
                    run_id, requirement_code, project_name, request_text,
                    agent_name, output, system_prompt, version, is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'specification', ?, ?, ?, 1, ?, ?)
                """,
                (
                    run_id,
                    current["requirement_code"],
                    current["project_name"],
                    current["request_text"],
                    specification,
                    current["system_prompt"],
                    int(current["version"]) + 1,
                    timestamp,
                    timestamp,
                ),
            )
            return {
                "run_id": run_id,
                "requirement_code": current["requirement_code"],
                "project_name": current["project_name"],
                "version": int(current["version"]) + 1,
            }

        return self._with_connection(update)

    def save_agent_output(
        self,
        *,
        run_id: str,
        agent_name: str,
        output: Any,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Version and replace one agent's output within an existing run."""
        timestamp = self._now()

        def update(conn):
            cursor = conn.execute(
                "SELECT requirement_code, project_name, request_text, system_prompt, version "
                "FROM prompt_output WHERE run_id = ? AND agent_name = ? AND is_current = 1 "
                "ORDER BY version DESC LIMIT 1",
                (run_id, agent_name),
            )
            current_rows = self._rows(cursor)
            if not current_rows:
                if agent_name != "repo_context":
                    raise LookupError("No saved output was found for that agent and run.")
                # Older runs predate the context agent. Attach its first version
                # using existing run metadata, never create an orphaned run.
                current_rows = self._rows(conn.execute(
                    "SELECT requirement_code, project_name, request_text, system_prompt, 0 AS version "
                    "FROM prompt_output WHERE run_id = ? AND is_current = 1 LIMIT 1", (run_id,),
                ))
                if not current_rows:
                    raise LookupError("The saved run was not found.")
            current = current_rows[0]
            next_version = int(current["version"]) + 1
            conn.execute(
                "UPDATE prompt_output SET is_current = 0, updated_at = ? "
                "WHERE run_id = ? AND agent_name = ? AND is_current = 1",
                (timestamp, run_id, agent_name),
            )
            conn.execute(
                """
                INSERT INTO prompt_output (
                    run_id, requirement_code, project_name, request_text,
                    agent_name, output, system_prompt, version, is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    run_id,
                    current["requirement_code"],
                    current["project_name"],
                    current["request_text"],
                    agent_name,
                    self._serialize(output),
                    current["system_prompt"] if system_prompt is None else system_prompt,
                    next_version,
                    timestamp,
                    timestamp,
                ),
            )
            return {
                "run_id": run_id,
                "agent_name": agent_name,
                "version": next_version,
            }

        return self._with_connection(update)
