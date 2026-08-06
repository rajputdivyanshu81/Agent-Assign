# Minerva - Autonomous E-Commerce Comparison Agent

Minerva is a transparent browser agent specifically tuned for e-commerce workflows. A user enters a plain-English product search goal, the backend runs an LLM-driven observe-think-act loop in Playwright to scrape and compare products across various online stores, and the frontend streams the agent's reasoning, actions, page screenshots, and final structured result.

## What To Try

Good demo goals:

- `Search the web to find and compare the price of 'Sony WH-1000XM5' headphones across different online stores.`
- `Find the cheapest price for an 'iPhone 15 Pro 256GB' from at least three different e-commerce retailers and summarize.`
- `Look up the best deals for a 'Dyson V15 Detect' vacuum on BestBuy and Amazon, comparing prices and shipping notes.`

The agent is not a hardcoded script. Each step is selected by the LLM from the current URL, visible interactive elements, readable page text, action history, and the user's goal, with specialized rules for navigating e-commerce sites.

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

## Recent Updates

- **Anti-bot Resilience**: Updated the system prompt to explicitly avoid Google and DuckDuckGo which aggressively block headless browsers. The agent now defaults to Bing or direct URLs for e-commerce sites to prevent false CAPTCHA triggers.
- **API Rate Limiting Mitigation**: Increased exponential backoffs in the event of `HTTP 429 Too Many Requests` (e.g. from Groq's free tier) to allow rate limits to reset automatically instead of crashing.
- **Frontend UI Enhancements**: Cleaned up the interactive DOM snapshot view to filter out noisy non-semantic tags (like `SVG`, `PATH`, `DIV`, `SPAN`) ensuring the live page state is clean and readable.
- **Next.js Stability**: Handled Next.js `.next` cache corruption issues on Windows virtual drives to prevent the dev server from crashing.
