from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from hub.config import Settings
from hub.services.runtime import AgentRuntimeState

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


def runtime_state(settings: Settings) -> AgentRuntimeState:
    return AgentRuntimeState(settings.resolved_runtime_dir)


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

    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


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


async def handle_lifecycle(action: str, request: Request, x_yeying_instance: str | None) -> dict:
    settings = Settings()
    state = runtime_state(settings)
    payload = await request_payload(request)
    try:
        task = state.create_task(action, payload, x_yeying_instance)
    except ValueError as exc:
        return {"code": 400, "message": str(exc), "data": {}}
    return ok({"task": task})


@router.get("/installed")
async def internal_installed(
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return ok(runtime_state(settings).installed())


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
    event = runtime_state(settings).record_hook(action, await request_payload(request), x_yeying_instance)
    return ok({"event": event})


@router.get("/project/agents")
async def internal_project_agents(
    authorization: str | None = Header(default=None),
    token: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_internal_token(settings, authorization, token)
    return ok(runtime_state(settings).installed())


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
    event = runtime_state(settings).record_hook(action, await request_payload(request), x_yeying_instance)
    return ok({"event": event})
