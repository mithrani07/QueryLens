# QueryLens

🚀 **Live Demo:** [https://querylens-ex3m.onrender.com](https://querylens-ex3w.onrender.com)
=======
🚀 **Live Demo:** [https://querylens-ex3m.onrender.com](https://querylens-ex3m.onrender.com)

**Talk to your data with SQL.** Upload a CSV/Excel/JSON file or connect a
PostgreSQL database, ask a question in plain English, and get back a
validated, executable SQL query with a plain-language explanation and a
live result preview.

```
"Find employees with salary above 10 lakh"
        │
        ▼
SELECT * FROM employees WHERE salary > 1000000;
```

---

## Features

- **Two data sources** — drag-and-drop a `.csv` / `.xlsx` / `.xls` / `.json`
  file, or connect directly to a PostgreSQL database with a connection
  string.
- **Natural language → SQL** — an LLM (Groq or NVIDIA NIM, both
  OpenAI-compatible) turns your question into a single SQL statement plus
  a short explanation.
- **Safety before execution** — every generated query is parsed with
  `sqlglot`, checked against an AST- and keyword-level block list for
  destructive statements (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`,
  `TRUNCATE`, …), and restricted to a single read-only statement before it
  ever touches your data.
- **One automatic self-correction** — if the generated SQL fails
  validation or execution, QueryLens feeds the error back to the model for
  a single corrected attempt before surfacing a failure.
- **Live result preview** — the validated query is actually run (against
  an in-memory DuckDB context) so you can see real rows before copying the
  SQL elsewhere.
- **Zero frontend build step** — the UI is plain HTML/CSS/JS with no
  bundler, framework, or `node_modules`.

## UI breakdown

| Panel | What it does |
|---|---|
| **Connect your data** | Tabs between *File upload* (drag-and-drop zone) and *Database* (PostgreSQL connection string + schema field). Once a source is loaded, a badge shows `📊 filename.csv` (or `🗄️ database@host`) with a chip per detected table. |
| **Ask your question** | A textarea (disabled until a source is loaded) plus example suggestion chips you can click to prefill a question. `Cmd/Ctrl + Enter` submits without leaving the keyboard. |
| **Generate SQL** | Primary action button; shows a pulsing icon and "Generating…" label while a request is in flight. |
| **Output** | Skeleton loading state → generated SQL (Prism.js-highlighted, with a one-click **Copy** button that shows a checkmark) → 2–3 line explanation (fades in) → an optional result preview table → or a red error banner if anything failed. |

All of this is wired up in `frontend/script.js`, organized into four
modules:

- **`State`** — the current source, schema, question, and loading flags.
- **`API`** — `uploadFile()`, `connectDatabase()`, `generateSQL()`, each
  with a request timeout and normalized error messages.
- **`UI`** — every DOM read/write (badges, Prism highlighting, copy
  feedback, toasts, error banners).
- **`Events`** — wires drag-and-drop, file picker, tab switching, form
  submission, and button clicks to the above.

## Project structure

```
querylens/
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
├── backend/
│   ├── main.py
│   ├── routes/            # upload.py, database.py, query.py
│   ├── services/          # file_service, schema_service, database_service,
│   │                       # llm_service, sql_service
│   ├── prompts.py
│   ├── config.py
│   └── requirements.txt
├── uploads/                # ephemeral working storage, .gitkeep only
├── .env
├── .gitignore
└── render.yaml
```

## Local setup

**Prerequisites:** Python 3.11+, a free [Groq](https://console.groq.com)
account.

1. **Clone and enter the backend directory**

   ```bash
   git clone <your-fork-url> querylens
   cd querylens/backend
   ```

2. **Create and activate a virtual environment**

   ```bash
   python3 -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Configure environment variables**

   Copy `.env` into `backend/.env` (pydantic-settings loads it from the
   project root relative to `config.py`) and fill in your Groq key — see
   [Getting a free Groq API key](#getting-a-free-groq-api-key) below.

5. **Run the app**

   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   Open **http://localhost:8000** — the frontend is served at `/`, the API
   under `/api/*`, and interactive API docs at `/docs`.

6. **Try it**: drag in a CSV, wait for the table chip to appear, then ask
   something like "show the top 5 rows" or "count rows grouped by
   category."

## Getting a free Groq API key

1. Go to [console.groq.com](https://console.groq.com) and sign in (GitHub
   or Google both work).
2. Open **API Keys** in the left sidebar and click **Create API Key**.
3. Name it (e.g. `querylens-dev`) and copy the key immediately — Groq only
   shows it once. It starts with `gsk_`.
4. Paste it into your `.env` as `GROQ_API_KEY=gsk_...`. Leave
   `GROQ_BASE_URL` and `GROQ_MODEL` at their defaults unless you want a
   different model from Groq's [model list](https://console.groq.com/docs/models).
5. Groq's free tier is generous for development but rate-limited; if you
   hit `429` errors, wait a minute or switch `LLM_PROVIDER=nvidia` and
   supply an [NVIDIA NIM](https://build.nvidia.com) key instead — the app
   treats both providers identically since both expose OpenAI-compatible
   endpoints.

**Never commit your real key.** `.gitignore` already excludes `.env`;
keep it that way.

## Deploying to Render (free tier)

This repo includes a ready-to-use `render.yaml` Blueprint that deploys the
FastAPI backend (which also serves the static frontend) as a single free
web service.

1. **Push your repo to GitHub** (Render deploys from a Git remote).

2. **Create the Blueprint**
   - In the Render dashboard, click **New +** → **Blueprint**.
   - Connect the GitHub repo containing `render.yaml`.
   - Render reads `render.yaml` and proposes the `querylens` web service
     on the `free` plan automatically.

3. **Provide the secret values**
   - Render will prompt for the two variables marked `sync: false`:
     `GROQ_API_KEY` (required) and `NVIDIA_API_KEY` (only needed if you
     switch `LLM_PROVIDER` to `nvidia`).
   - Everything else (model names, timeouts, CORS, upload limits) is
     already set from `render.yaml`.

4. **Deploy**
   - Click **Apply**. Render will run the build command
     (`pip install -r requirements.txt`) and then the start command
     (`uvicorn main:app --host 0.0.0.0 --port $PORT`).
   - Watch the **Logs** tab; a successful boot logs
     `Starting QueryLens v1.0.0 in production mode (LLM provider: groq, ...)`.

5. **Verify**
   - Visit `https://<your-service>.onrender.com/health` — you should get
     `{"status": "ok", ...}`.
   - Visit the root URL to load the UI itself.

6. **Free-tier caveats**
   - The service spins down after ~15 minutes of inactivity; the next
     request wakes it up but will be noticeably slower (cold start).
   - The filesystem is ephemeral — anything under `UPLOAD_DIR` (i.e.
     uploaded files) is wiped on every redeploy or restart. That's
     expected for QueryLens; uploads are meant to be short-lived working
     data, not permanent storage.
   - If you later split the frontend into its own Render Static Site,
     change `CORS_ORIGINS` from `["*"]` to that site's exact URL.

## Security notes

QueryLens treats generated SQL as untrusted by default: every query is
parsed, type-checked against an allow-list of read-only statement types,
scanned for destructive keywords, and executed against an isolated
in-memory DuckDB context with external file/network access disabled
before any result is returned. See the project's security audit for the
full list of mitigations and known limitations (e.g. very large connected
Postgres tables are materialized in full for the execution preview, which
is a memory consideration on constrained hosts).
