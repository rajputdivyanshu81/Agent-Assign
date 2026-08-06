# Minerva

Minerva is a transparent browser agent for research workflows. A user enters a plain-English goal, the backend runs an LLM-driven observe-think-act loop in Playwright, and the frontend streams the agent's reasoning, actions, page screenshots, controls, and final structured result.

## What To Try

Good demo goals:

- `Research Slack, Notion, and Asana pricing across their official websites and produce a structured comparison.`
- `Compare Linear and Jira for issue tracking using at least two sources and summarize tradeoffs for a small product team.`
- `Find current public pricing details for GitHub Copilot and Cursor, then compare plans and limitations.`

The agent is not a hardcoded script. Each step is selected by the LLM from the current URL, visible interactive elements, readable page text, action history, and the user's goal.

## Local Run

Backend:

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
copy ..\.env.example ..\.env
# Fill DATABASE_URL in ..\.env; provider keys are entered in the app per run
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Frontend:

```bash
cd frontend
npm install
$env:NEXT_PUBLIC_BACKEND_URL="http://localhost:8000"
npm run dev
```

Open `http://localhost:3000`.

## Deployment

The frontend can run on Vercel. Set:

```text
NEXT_PUBLIC_BACKEND_URL=https://your-backend-host.example
```

The frontend converts `https://` to `wss://` for the WebSocket connection.

The backend is containerized in `backend/Dockerfile` using Microsoft's Playwright image, so Chromium and its Linux runtime libraries are available on the server. `render.yaml` provides a Render blueprint for the backend web service.

Backend environment variables:

```text
DATABASE_URL=...
MINERVA_HEADLESS=true
MINERVA_BROWSER_USER_DATA_DIR=/tmp/minerva-browser
```

After deploy, verify `GET /` returns:

```json
{"status":"Minerva Agent Backend is running"}
```

## Human Control

The UI supports:

- Pause and resume while a run is active.
- Stop to halt the current run.
- Step-by-step approval mode, enabled before starting a run, requiring approval before each browser action.

When the agent detects anti-bot protection, login walls, repeated actions, invalid actions, or step limits, it streams the failure reason instead of silently hanging.
