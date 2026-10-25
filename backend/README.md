# Backend

Python/FastAPI control plane, packaged as `agent-hub`.

From the repository root, install and run it with:

```bash
(cd backend && uv sync)
test -f backend/.env || cp backend/.env.template backend/.env
cd backend
uv run python -m uvicorn hub.app:create_app --factory --reload --host 127.0.0.1 --port 3900
```

The local `.env` is ignored by Git. See the [root README](../README.md) for frontend startup and [operations docs](../docs/operations.md) for deployment.
