# Backend

Python/FastAPI control plane, packaged as `agent-hub`.

From the repository root, install and run it with:

```bash
(cd backend && uv sync)
test -f config/hub.env || cp config/hub.env.template config/hub.env
set -a
source config/hub.env
set +a
(cd backend && uv run alembic upgrade head)  # HUB_DATABASE_URL 已配置时执行
bash scripts/starter.sh start
```

The control plane runs as a native Python process managed by `scripts/starter.sh`; Docker is not used to start Orchestrator. See the [root README](../README.md) for frontend startup and [operations docs](../docs/operations.md) for deployment. Stop it with `bash scripts/starter.sh stop`.

For production control-plane state, set `HUB_DATABASE_URL` in `config/hub.env` to PostgreSQL, source that file from the repository root, then apply the versioned schema before starting the service:

```bash
set -a
source config/hub.env
set +a
(cd backend && uv run alembic upgrade head)
```

The database mode stores control-plane metadata and task state. OpenClaw profiles, active workspaces, logs, and other process-local files remain on the configured node volumes. See [runtime data planning](../docs/runtime-data.md) for the migration and backup boundary.

When Warehouse archiving is configured with `YEYING_WAREHOUSE_URL` and a short-lived scoped `YEYING_WAREHOUSE_TOOL_TOKEN`, run the outbox worker separately:

```bash
uv run orchestrator-outbox-worker
```

After correcting a permanent Warehouse or authorization failure, explicitly requeue failed records with `uv run orchestrator-outbox-worker --retry-failed --once`.

## Application lifecycle worker

`orchestrator-runtime-worker` consumes persisted install, upgrade, and uninstall tasks. It is disabled unless `HUB_RUNTIME_EXECUTOR_ENABLED=true`; the node must have Docker Compose v2, PostgreSQL, a Node Runtime Agent credential, and trusted publisher public keys configured in `config/hub.env`. Transient Node/Docker/healthcheck errors use persistent exponential retry scheduling, bounded by `HUB_WORKER_MAX_ATTEMPTS`. Configure `HUB_APPLICATION_CONFIG_DIR` with per-Project/app JSON files owned by the service account and mode `0600` for values referenced by `runtime.json.environment`. The JSON maps allowed source names such as `DB_HOST` or `DB_PASSWORD` to strings; `APP_URL` is derived by the worker. Its owner directory is the first 24 hex characters of SHA-256 over the `X-YeYing-Instance` value. Do not put credentials in lifecycle request payloads.

Start one worker on the deployment node:

```bash
set -a
source config/hub.env
set +a
(cd backend && uv run orchestrator-runtime-worker)
```

The executor verifies the exact published release and Ed25519 signature, then generates its own constrained Compose definition; it never executes the publisher's Compose file. Containers bind only to loopback. Configure a separate reverse proxy before exposing apps. Release `host_port` is static today, so only one deployment may use the same port on a node; multi-Project port reservations and tenant-aware routing remain future work. `latest` is not accepted. Keep the worker disabled until those operational prerequisites are reviewed.
