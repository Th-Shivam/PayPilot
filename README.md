# PayPilot

PayPilot is a transaction reconciliation assistant. The frontend is a React/Vite app and the backend is a FastAPI service.

## Prerequisites

- Node.js 20 or newer
- Python 3.11 or newer

## Local setup

1. Install frontend dependencies:

   ```bash
   cd frontend
   npm install
   ```

2. Create a backend virtual environment and install dependencies:

   ```bash
   python3 -m venv backend/.venv
   backend/.venv/bin/python -m pip install -r backend/requirements.txt
   ```

3. Copy the environment template and fill in credentials only for services you are using:

   ```bash
   cp backend/.env.example backend/.env
   ```

   The backend also accepts a repository-root `.env` when you prefer keeping one
   local environment file for the project.

   Local development defaults to `APP_ENV=local`, so the backend can start without integration credentials. Set `APP_ENV=production` only when all Groq, Supabase, and Logfire variables are present.

## Run locally

Start the backend from the repository root:

```bash
backend/.venv/bin/python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Start the frontend in a second terminal:

```bash
cd frontend
npm run dev
```

The frontend uses `https://paypilot-ki2b.onrender.com` by default in every environment. To point it at a local backend during development, create `frontend/.env.local` with `VITE_API_BASE_URL=http://127.0.0.1:8000` and restart Vite.

Production frontend builds use the deployed API at `https://paypilot-ki2b.onrender.com` by default. Set `VITE_API_BASE_URL` in the frontend hosting provider when using a different backend URL. The backend's `ALLOWED_ORIGINS` must include the final frontend origin (without a trailing slash) for browser requests to succeed.

Frontend checks run from the frontend directory:

```bash
cd frontend
npm run build
npm run lint
```

## API checks

- Health endpoint: `GET http://127.0.0.1:8000/health`
- Interactive API docs: `http://127.0.0.1:8000/docs`

The health endpoint returns a stable response such as:

```json
{
  "status": "ok",
  "service": "PayPilot API",
  "environment": "local"
}
```

## Configuration safety

Never commit `.env` or `.env.local`. They are ignored by Git. Use `backend/.env.example` as the shareable list of variable names. Production startup fails with the names of missing required variables, without printing secret values.

## Observability

Set `LOGFIRE_TOKEN` to trace resolves in [Logfire](https://logfire.pydantic.dev); leave it unset and PayPilot logs locally instead, with the resolve path unchanged. Every response carries `X-Request-Id`, every error body repeats it as `request_id`, and pasting that id into Logfire returns the whole run. Customer identity and credentials are stripped before anything reaches a span; amounts and statuses are kept, because they are the reconciliation signal. See [docs/observability.md](docs/observability.md).
