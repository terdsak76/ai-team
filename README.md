# AI Agent Team

## Phase 1 architecture

`main.py` now enters the Muse orchestration layer. Muse builds and validates a
task DAG, prepares read-only repository context, forecasts resource conflicts,
reserves task resources, and schedules work through the in-process Muse task
queue. The OpenAI Agents SDK runs the five task agents in this order:

```text
             -> ui/ux -> frontend -\
specification                         -> tester
             \-> backend -------------/
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

Then open <http://127.0.0.1:8000>. All five agents use the configured OpenRouter client and `OPENROUTER_API_KEY`
from `.env`; an `OPENAI_API_KEY` is not required. Prompts can be edited per
agent in the browser, or saved as project master data from the Projects menu.
A project stores its name, GitHub repository, private-repository token, and
the system prompt for each of the five agents. The token is kept server-side
and is not returned to the browser. Selecting a project for a run applies its
saved repository connection and prompts.

To give agents read-only access to a GitHub repository, enter its URL in the
workspace, such as `https://github.com/owner/repository`, or save it in a
project master record. Public repositories work without a token. Private
repositories can use the project token; the server-level `GITHUB_TOKEN`
environment variable remains available for manually entered repositories.
The token stays on the server. Agents can list and read UTF-8 files up to 40 KB;
generated directories and common secret files are excluded. Repository code
that agents read is sent to the configured model provider as part of the run.

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
`/api/agent`, `/api/outputs`, and `/api/specification`, so no public API URL or client-side
secret is required.

## Turso persistence

Project master records are saved to the `project` table. Completed runs are saved to the `prompt_output` table. Each run stores one
row for the specification, UI/UX design, frontend, backend, and tester output.
The table is created automatically from the server using these environment
variables:

- `TURSO_URL`
- `TURSO_TOKEN`

Use the workspace fields to provide a requirement code and project name. The
saved-output search accepts either value. Loading a run makes its functional
specification editable; saving an edit creates a new version while preserving
the previous database row.

After loading a run, the UI/UX, frontend, and backend cards each have a
standalone run action. Each action uses the selected specification and saves a
new version of only that agent's output.
