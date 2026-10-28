from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request

from orchestrator.config import Settings
from orchestrator.models import RuntimeNodeHeartbeatRequest, RuntimeNodeListResponse
from orchestrator.services.nodes import NODE_ID_PATTERN, RuntimeNodeService
from orchestrator.services.runtime import AgentRuntimeState

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


def runtime_state(settings: Settings) -> AgentRuntimeState:
    return AgentRuntimeState(settings.resolved_runtime_dir, settings.database_url)


def require_internal_token(
    settings: Settings,
    authorization: str | None,
    legacy_token: str | None,
) -> None:
    expected = settings.internal_token.strip()
    if expected == "":
        raise HTTPException(status_code=503, detail="internal token is not configured")

    bearer_prefix = "Bearer "
    provided = ""
    if authorization and authorization.startswith(bearer_prefix):
        provided = authorization[len(bearer_prefix) :].strip()
    elif legacy_token:
        provided = legacy_token.strip()

    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


def require_admin_token(settings: Settings, authorization: str | None) -> None:
    expected = settings.admin_token.strip()
    if not expected or expected == "change-me-admin-token":
        raise HTTPException(status_code=503, detail="admin token is not configured")

    bearer_prefix = "Bearer "
    provided = authorization[len(bearer_prefix) :].strip() if authorization and authorization.startswith(bearer_prefix) else ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_worker_token(settings: Settings, authorization: str | None) -> None:
    expected = settings.worker_token.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="worker token is not configured")
    provided = authorization[len("Bearer ") :].strip() if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid worker token")


def require_runtime_node_token(settings: Settings, node_id: str, authorization: str | None) -> None:
    if not NODE_ID_PATTERN.fullmatch(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    raw_tokens = settings.runtime_node_tokens.strip()
    if not raw_tokens:
        raise HTTPException(status_code=503, detail="node agent credentials are not configured")
    try:
        token_map = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="node agent credentials are misconfigured") from exc
    if not isinstance(token_map, dict):
        raise HTTPException(status_code=503, detail="node agent credentials are misconfigured")

    configured = token_map.get(node_id)
    tokens = [configured] if isinstance(configured, str) else configured if isinstance(configured, list) else []
    if not tokens:
        raise HTTPException(status_code=401, detail="invalid node agent credentials")
    if len(tokens) > 3:
        raise HTTPException(status_code=503, detail="at most three active credentials may be configured per runtime node")
    if any(not isinstance(token, str) or len(token) < 32 for token in tokens):
        raise HTTPException(status_code=503, detail="node agent credential must be at least 32 characters")

    bearer_prefix = "Bearer "
    provided = authorization[len(bearer_prefix) :].strip() if authorization and authorization.startswith(bearer_prefix) else ""
    valid = False
    for expected in tokens:
        valid = hmac.compare_digest(provided, expected) or valid
    if not valid:
        raise HTTPException(status_code=401, detail="invalid node agent credentials")


def require_worker_attempt(value: int | None) -> int:
    if value is None or value < 1:
        raise HTTPException(status_code=400, detail="X-Worker-Attempt is required")
    return value


def bounded_payload_int(payload: dict, name: str, default: int) -> int:
    try:
        return int(payload.get(name, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from exc


async def request_payload(request: Request) -> dict:
    if request.method == "GET":
        return {}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return payload if isinstance(payload, dict) else {}


def ok(data: dict | list, message: str = "success") -> dict:
    return {"code": 200, "message": message, "data": data}


@router.post("/runtime-nodes/{node_id}/heartbeat")
async def internal_runtime_node_heartbeat(
    node_id: str,
    payload: RuntimeNodeHeartbeatRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_runtime_node_token(settings, node_id, authorization)
    try:
        node = RuntimeNodeService(
            settings.database_url, settings.runtime_node_offline_after_seconds
        ).heartbeat(node_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ok({"node": node.model_dump()})


@router.get("/runtime-nodes")
async def internal_runtime_nodes(
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    try:
        nodes: RuntimeNodeListResponse = RuntimeNodeService(
            settings.database_url, settings.runtime_node_offline_after_seconds
        ).list_nodes()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ok(nodes.model_dump())


async def handle_lifecycle(action: str, request: Request, x_yeying_instance: str | None) -> dict:
    settings = Settings()
    state = runtime_state(settings)
    payload = await request_payload(request)
    try:
        task = state.create_task(action, payload, x_yeying_instance, request.headers.get("x-request-id"))
    except ValueError as exc:
        return {"code": 400, "message": str(exc), "data": {}}
    return ok({"task": task})


@router.get("/installed")
async def internal_installed(
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return ok(runtime_state(settings).installed(x_yeying_instance))


@router.get("/tasks/{task_id}")
async def internal_task(
    task_id: str,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    task = runtime_state(settings).get_task(task_id)
    if task is None or task.get("owner_id", task.get("instance_id", "project")) != str(x_yeying_instance or "project"):
        raise HTTPException(status_code=404, detail="task not found")
    return ok({"task": task})


@router.post("/tasks/claim")
async def internal_claim_tasks(
    request: Request,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_worker_token(settings, authorization)
    payload = await request_payload(request)
    worker_id = str(x_worker_id or "").strip()
    try:
        tasks = runtime_state(settings).claim_tasks(
            worker_id,
            limit=bounded_payload_int(payload, "limit", 1),
            lease_seconds=bounded_payload_int(payload, "lease_seconds", 60),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok({"tasks": tasks})


@router.post("/tasks/{task_id}/heartbeat")
async def internal_task_heartbeat(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
    x_worker_attempt: int | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_worker_token(settings, authorization)
    payload = await request_payload(request)
    try:
        task = runtime_state(settings).heartbeat_task(
            task_id,
            str(x_worker_id or ""),
            require_worker_attempt(x_worker_attempt),
            bounded_payload_int(payload, "lease_seconds", 60),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409 if "lease" in str(exc) else 404, detail=str(exc)) from exc
    return ok({"task": task})


@router.post("/tasks/{task_id}/release")
async def internal_task_release(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
    x_worker_attempt: int | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_worker_token(settings, authorization)
    payload = await request_payload(request)
    try:
        task = runtime_state(settings).release_task(
            task_id,
            str(x_worker_id or ""),
            require_worker_attempt(x_worker_attempt),
            payload.get("reason"),
            retry_delay_seconds=bounded_payload_int(payload, "retry_delay_seconds", 0),
            request_id=request.headers.get("x-request-id"),
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "lease" in message or "owned" in message else 404 if "not found" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    return ok({"task": task})


@router.post("/tasks/{task_id}/result")
async def internal_task_result(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
    x_worker_attempt: int | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_worker_token(settings, authorization)
    payload = await request_payload(request)
    try:
        task = runtime_state(settings).finish_task(
            task_id,
            str(x_worker_id or ""),
            require_worker_attempt(x_worker_attempt),
            str(payload.get("outcome") or ""),
            result_code=payload.get("result_code"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            request_id=request.headers.get("x-request-id"),
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "lease" in message or "owned" in message else 404 if "not found" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc
    return ok({"task": task})


@router.post("/install")
async def internal_install(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("install", request, x_yeying_instance)


@router.post("/upgrade")
async def internal_upgrade(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("upgrade", request, x_yeying_instance)


@router.post("/uninstall")
async def internal_uninstall(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("uninstall", request, x_yeying_instance)


@router.post("/hooks/{action}")
async def internal_hook(
    action: str,
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    event = runtime_state(settings).record_hook(action, await request_payload(request), x_yeying_instance, request.headers.get("x-request-id"))
    return ok({"event": event})


@router.get("/project/agents")
async def internal_project_agents(
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return ok(runtime_state(settings).installed(x_yeying_instance))


@router.post("/project/agents/install")
async def internal_project_agents_install(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("install", request, x_yeying_instance)


@router.post("/project/agents/upgrade")
async def internal_project_agents_upgrade(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("upgrade", request, x_yeying_instance)


@router.post("/project/agents/uninstall")
async def internal_project_agents_uninstall(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return await handle_lifecycle("uninstall", request, x_yeying_instance)


@router.post("/project/hooks/{action}")
async def internal_project_hook(
    action: str,
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
    x_yeying_instance: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    event = runtime_state(settings).record_hook(action, await request_payload(request), x_yeying_instance, request.headers.get("x-request-id"))
    return ok({"event": event})
