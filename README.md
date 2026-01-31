# FactTrace

Multi-agent fact-checking system with a Python backend and a React frontend.
The backend runs a structured jury debate over claims; the frontend streams the
debate and renders the final verdict.

## Repository layout

- `checker-of-claims/` — Python backend + CLI (core debate engine)
- `truth-jury-frontend/` — React + Vite frontend
- `cambridge-dis-hackathon/` — Hackathon prompt + dataset materials
- `hackathon.md` — Local notes (currently empty)

## Quickstart (backend only)

```bash
cd checker-of-claims
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env with your keys

factcheck "The Moon is made of cheese."
```

## Quickstart (full stack)

### 1) Backend API

```bash
cd checker-of-claims
python -m venv .venv
source .venv/bin/activate
pip install -e .

export OPENAI_API_KEY="your-key-here"
# or create .env with OPENAI_API_KEY=...

python -m checker_of_facts.api
```

API: `http://localhost:8000`

### 2) Frontend

```bash
cd truth-jury-frontend
npm install
npm run dev
```

Frontend: `http://localhost:5173`

## Environment variables

### Backend
- `OPENAI_API_KEY` (required)
- `OPENAI_MODEL` (optional, default `gpt-4.1-mini`)
- `PORT` (optional, default `8000`)

### Frontend
- `VITE_API_URL` (optional, default `http://localhost:8000`)

## Tests

### Backend
```bash
cd checker-of-claims
.venv/bin/python -m pytest -q
```

### Frontend
```bash
cd truth-jury-frontend
npm run test
```

## Documentation

- `checker-of-claims/README.md` — Backend CLI usage
- `checker-of-claims/HACKATHON_README.md` — Hackathon jury debate flow
- `checker-of-claims/INTEGRATION_README.md` — End-to-end integration guide
