# AI Agent Team

## Phase 1 architecture

`main.py` now enters the Muse orchestration layer. Muse builds and validates a
task DAG, prepares read-only repository context, forecasts resource conflicts,
reserves task resources, and schedules work through the in-process Muse task
queue. A separate read-only Repo Context Architect runs before the five task
agents, in this order:

```text
index -> repo context LLM -> persist -> specification
                                        |-> ui/ux -> frontend -\
                                        \-> backend ------------> tester
```

The UI/UX agent produces a Figma-ready design specification before frontend
implementation. Backend remains an independent branch after the functional
specification, and the tester waits for both implementations. The queue is
intentionally an in-process adapter so the next phase can replace it with a
durable Muse queue without changing agent definitions or the web API.

Run the local web interface from the project root:

```bash
./.venv/bin/python web.py
```

Then open <http://127.0.0.1:8000>. All six agents use the configured OpenRouter client and `OPENROUTER_API_KEY`
from `.env`; an `OPENAI_API_KEY` is not required. Prompts can be edited per
agent in the browser, or saved as project master data from the Projects menu.
A project stores its name, GitHub repository, private-repository token, and
the system prompt for each of the five agents. The token is kept server-side
and is not returned to the browser. Selecting a project for a run applies its
saved repository connection and prompts. Project master data also includes a
compact context summary. New project runs retrieve relevant durable memories
and provide the summary and memories to the Specification Agent before the
new task is processed.

To give agents read-only access to a GitHub repository, enter its URL in the
workspace, such as `https://github.com/owner/repository`, or save it in a
project master record. Public repositories work without a token. Private
repositories can use the project token; the server-level `GITHUB_TOKEN`
environment variable remains available for manually entered repositories.
The token stays on the server. Agents can list and read UTF-8 files up to 40 KB;
generated directories and common secret files are excluded. Repository code
that agents read is sent to the configured model provider as part of the run.

Connected repositories are indexed initially, then their cached context is checked
before each run. The bounded metadata index
uses Tree-sitter for Python, Java, JavaScript/TypeScript, TSX, and SQL, with a
safe regex fallback when a grammar is unavailable. It records imports, classes,
interfaces, methods, annotations, database tables, columns, keys, views,
routines, indexes, and source line ranges. Agents receive the compact index and
can use symbol search plus targeted source reads instead of loading whole files.
Each indexed revision is persisted in Turso as a versioned repository snapshot,
its generated artifacts, and normalized ORM/database relationship edges. The
snapshot tables are `repo_map_snapshot`, `repo_map_artifact`, and
`repo_relationship`.

### Repo Context agent and panel

After indexing, the **Repo Context Architect** creates a bounded semantic
architecture summary: component responsibilities, data flow, dependencies,
ORM/database mappings, conventions, and coverage gaps. It receives the compact
index and read-only repository tools for targeted reads, not database credentials
or application rows. Its fixed analysis policy lives in
`team_agents/repo_context.py`; the five development prompts remain editable.
Observed evidence, inferences, and unknowns must be distinguished. The summary
is included in every development agent's context and persisted in
`.repo-map/architecture.md` alongside the deterministic graph. This additional
LLM agent stage runs only when context must be built or refreshed.
Without a repository or database source this stage is skipped.

The **Repo Context** panel above the development-agent cards shows cache checks, indexing,
summarizing, saving, and ready status; file/symbol/database counts; the semantic
summary; artifact previews/downloads; and an evidence/confidence table of graph
edges. Partial indexing coverage is flagged. Full runs request NDJSON progress
from `/api/run`; ordinary JSON clients are still supported. The panel restores
the exact snapshot attached to a saved run, not the latest repository snapshot.
Old runs without context show **Unavailable**. Standalone reruns update and
version their saved context report too, reusing a valid project snapshot.

#### Context caching and refresh

Full and standalone runs first check the project's current Turso snapshot.
For GitHub, only repository metadata and the default branch's current commit
SHA are fetched on a cache hit: file downloads, indexing, schema inspection,
and architecture-model requests are skipped. Both indexing and later targeted
source reads are pinned to that immutable commit, not a moving branch name.
The new requirement text is not part of the architecture cache key.

The cache is scoped by project and repository (workspace runs have their own
scope). Changed database connection settings, analysis policy/model, or GitHub
commit cause a rebuild. Cache metadata is stored inside `context_json`; old
snapshots without it rebuild once, then become reusable. Repository-only
context remains reusable while its commit/settings are unchanged. Context
containing any live database schema expires **one hour after creation**, since
schema migrations may happen independently of GitHub. Reuse does not extend
that expiry. GitHub access failures are reported, not masked with stale context.

The panel shows **Reused** or **Rebuilt**, the reason, generation time, and schema
expiry when applicable. **Refresh Repo Context** forces a fresh index/schema
read and semantic summary immediately for the selected saved project (including
database-only projects), or the workspace repository. It does not require a new
requirement or run the five development agents. New requirements use the refreshed
cache; old saved runs retain their original snapshot identifiers.

Programmatic callers can set `force_refresh_context=True` on `run_project` or
`run_single_agent`, or call `workflow.refresh_repo_context(project_id=...)`.
The matching API is `POST /api/repo-context` with `project_id` or `repository_url`
(optional `database_connections` for workspace callers), and optional
`stream: true` for progress. Saved project credentials are resolved server-side.

No additional SQL migration is needed for the agent/panel or cache: the summary uses
the existing snapshot JSON and artifact tables; its run report is another
`prompt_output` row. The panel reads immutable artifacts through
`GET /api/repo-context?snapshot_id=<id>`.

### Live database context

In **Projects**, enable **Read a server database**, **Read a Turso database**,
or both. Server databases support PostgreSQL, MySQL, and SQL Server. Enter the
host, optional port, database name, username, optional schema, and the name of
a server environment variable containing its password. Turso requires its URL
and the name of a server environment variable containing its token.

For example, configure secrets on the server:

```dotenv
APP_DB_PASSWORD=your_schema_reader_password
APP_TURSO_TOKEN=your_application_database_read_token
```

The form receives environment variable **names**, not passwords or tokens.
Use accounts that can read schema metadata. The connectors read table/view
catalogs, columns, primary keys, foreign keys, and indexes; they do not read
application rows or give agents a SQL execution tool. Database errors are
sanitized so driver connection strings and credentials are not returned.

Select the saved project for a full or standalone agent run. You can also pass
connections directly through `workflow.run_project` or `run_single_agent`:

```python
result = await run_project(
    "Add employee search",
    repository_url="https://github.com/owner/repository",
    database_connections=[
        {
            "name": "server", "type": "postgresql",
            "host": "database.example.com", "port": 5432,
            "database": "employees", "username": "schema_reader",
            "schema": "public", "password_env": "APP_DB_PASSWORD",
        },
        {
            "name": "turso", "type": "turso",
            "url": "libsql://your-app-database.turso.io",
            "token_env": "APP_TURSO_TOKEN",
        },
    ],
)
```

Set `type` to `mysql` or `sqlserver` for those server databases. Default ports
are 5432, 3306, and 1433 respectively. SQL Server uses the `pymssql` driver;
PostgreSQL uses `psycopg`, and MySQL uses `PyMySQL`. These drivers are included
in the project dependencies. Server reflection uses
[SQLAlchemy's catalog inspection API](https://docs.sqlalchemy.org/en/20/core/reflection.html).

Database context also works without a GitHub repository. Up to five sources
can be passed via the workflow/API. Omit `database_connections` to use saved
project settings; pass `[]` to disable database context for that run. Snapshots
include `database-schema.json` and observed foreign-key relationships. ORM table
mappings are linked to live schema targets only when the match is unambiguous.

`TURSO_URL` and `TURSO_TOKEN` still configure Agent Team's artifact/output
storage. The application databases read for context are separate connections;
reading a server database does not replace the Turso persistence backend.

The existing command-line workflow can also be run with:

```bash
./.venv/bin/python main.py
```

## Deploy to Vercel

This project includes Vercel serverless functions in `api/` and static-file
rewrites in `vercel.json`. From the project root:

```bash
npx vercel
```

Configure these environment variables in the Vercel project settings:

- `OPENROUTER_API_KEY` (required)
- `GITHUB_TOKEN` (optional; needed only for private repositories)
- `TURSO_URL` (required for persistence)
- `TURSO_TOKEN` (required for persistence)

The browser only calls same-origin `/api/prompts`, `/api/projects`, `/api/run`,
`/api/agent`, `/api/outputs`, `/api/repo-context`, and `/api/specification`, so no public API URL or client-side
secret is required.

## Turso persistence

Project master records are saved to the `project` table. Durable task memories
are saved to `project_memory`. Completed runs are saved to the
`prompt_output` table. Each run stores one
row for the Repo Context report, specification, UI/UX design, frontend, backend,
and tester output.
The table is created automatically from the server using these environment
variables:

- `TURSO_URL`
- `TURSO_TOKEN`

### SQL migrations for repository and database context

Apply these scripts to Agent Team's **storage** Turso database, identified by
`TURSO_URL`. They preserve existing project and agent-output records. The new
runtime code also creates these schema objects automatically.

1. Apply [001_repo_map_artifacts.sql](migrations/001_repo_map_artifacts.sql)
   to create repository snapshots, artifact documents, relationship edges,
   and their indexes. This script can be rerun.
2. Check whether the existing `project` table already has the settings column:

   ```bash
   turso db shell your-agent-team-db "PRAGMA table_info(project);"
   ```

3. If the table exists and **does not** contain `database_connections`, apply
   [002_project_database_connections.sql](migrations/002_project_database_connections.sql).
   Skip it if the column already exists. This migration must run only once.

From the project root, use the
[Turso CLI's SQL file input](https://docs.turso.tech/cli/db/shell):

```bash
turso db shell your-agent-team-db < migrations/001_repo_map_artifacts.sql
# Run the next command only after checking that the column is missing:
turso db shell your-agent-team-db < migrations/002_project_database_connections.sql
```

The migrations create storage structures; subsequent workflow runs populate
them with repo-map artifacts and database schema context.

Use the workspace fields to provide a requirement code and project name. The
saved-output search accepts either value. Loading a run makes its functional
specification editable; saving an edit creates a new version while preserving
the previous database row.

After loading a run, the UI/UX, frontend, and backend cards each have a
standalone run action. Each action uses the selected specification and saves a
new version of that agent's output and its refreshed Repo Context report.
