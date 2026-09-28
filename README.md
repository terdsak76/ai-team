# AI Agent Team

Run the local web interface from the project root:

```bash
./.venv/bin/python web.py
```

Then open <http://127.0.0.1:8000>. All four agents use the configured OpenRouter client and `OPENROUTER_API_KEY`
from `.env`; an `OPENAI_API_KEY` is not required. Prompts are editable per
agent and saved in the current browser;
the submitted task and prompts are used for that run only.

To give agents read-only access to a GitHub repository, enter its URL in the
workspace, such as `https://github.com/owner/repository`. Public repositories
work without a token. For private repositories, set `GITHUB_TOKEN` in the
server environment to a fine-grained token with read-only contents access.
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

The browser only calls same-origin `/api/prompts` and `/api/run`, so no public
API URL or client-side secret is required.
