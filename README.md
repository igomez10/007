# Hello Server

Minimal FastAPI server exposing `/hello` and `/health`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --reload
```

## Endpoints

- `GET /hello?name=<name>` → `{"message": "Hello, <name>!"}` (defaults to `world`)
- `GET /health` → `{"status": "ok"}`
- `GET /docs` → interactive API docs
