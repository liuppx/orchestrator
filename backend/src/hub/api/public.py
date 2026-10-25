from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import Query

from hub.adapters import AgentWorkspaceAdapter, get_agent_workspace_adapter
from hub.config import Settings
from hub.models import (
    AuthChallengeRequest,
    AuthChallengeView,
    BotInstanceActionResponse,
    AuthSessionView,
    AuthSessionVerifyRequest,
    BotInstanceCreateRequest,
    BotInstanceDiagnoseResponse,
    BotInstanceListResponse,
    BotInstanceLogsResponse,
    BotInstancePairResponse,
    BotInstanceUpdateModelRequest,
    BotInstanceView,
    HealthResponse,
    AgentListResponse,
    AgentWorkspaceActionResponse,
    AgentWorkspaceConfigUpdateRequest,
    AgentWorkspaceConfigUpdateResponse,
    AgentWorkspaceOrderUnlockRequest,
    AgentWorkspaceSummaryResponse,
    AgentTypesResponse,
    RouterModelsResponse,
    VersionResponse,
)
from hub.services import (
    MessengerStateService,
    AgentRegistry,
    SESSION_COOKIE_NAME,
    auth_service,
)
from hub.services.messenger import short_wallet

router = APIRouter(prefix="/api/v1/public", tags=["public"])


def require_session(request: Request) -> AuthSessionView:
    settings = Settings()
    session = auth_service(settings).current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return session


def owner_matches_wallet(owner_wallet: str | None, wallet_id: str) -> bool:
    if not owner_wallet:
        return False
    if owner_wallet == wallet_id:
        return True
    if owner_wallet.lower() == wallet_id.lower():
        return True
    return owner_wallet == short_wallet(wallet_id)


def require_owned_instance(
    service: MessengerStateService,
    instance_id: str,
    session: AuthSessionView,
) -> tuple[dict, dict]:
    record = service.get_instance_raw(instance_id)
    if record is None:
        raise HTTPException(status_code=404, detail="instance not found")
    _db, raw = record
    if not owner_matches_wallet(str(raw.get("owner_wallet", "")), session.wallet_id):
        raise HTTPException(status_code=404, detail="instance not found")
    return record


def messenger_service(settings: Settings) -> MessengerStateService:
    from hub.services.messenger import MessengerRuntimeConfig

    return MessengerStateService(
        MessengerRuntimeConfig(
            state_file=settings.resolved_runtime_dir / "state.json",
            instances_root=settings.resolved_instances_root,
            runtime_dir=settings.resolved_runtime_dir,
            repo_root=settings.repo_root,
            default_model=settings.default_model,
            model_allowlist=settings.parsed_model_allowlist,
            router_base_url=settings.router_base_url,
            router_api_key=settings.router_api_key,
            port_range_start=settings.instance_port_start,
            port_range_end=settings.instance_port_end,
            openclaw_prefix=settings.openclaw_prefix,
        )
    )
def workspace_adapter_for_agent(settings: Settings, agent_key: str) -> AgentWorkspaceAdapter:
    adapter = get_agent_workspace_adapter(settings.repo_root, agent_key)
    if adapter is None:
        raise HTTPException(status_code=404, detail="agent capability not found")
    return adapter


@router.get("/health", response_model=HealthResponse)
async def public_health() -> HealthResponse:
    return HealthResponse()


@router.get("/version", response_model=VersionResponse)
async def public_version() -> VersionResponse:
    return VersionResponse()


@router.get("/auth/me", response_model=AuthSessionView)
async def public_auth_me(request: Request) -> AuthSessionView:
    settings = Settings()
    session = auth_service(settings).current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return session


@router.post("/auth/wallet/challenge", response_model=AuthChallengeView)
async def public_auth_wallet_challenge(
    payload: AuthChallengeRequest,
    request: Request,
) -> AuthChallengeView:
    settings = Settings()
    service = auth_service(settings)
    try:
        return service.create_challenge(request, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/wallet/verify", response_model=AuthSessionView)
async def public_auth_wallet_verify(
    payload: AuthSessionVerifyRequest,
    request: Request,
    response: Response,
) -> AuthSessionView:
    settings = Settings()
    service = auth_service(settings)
    try:
        session = service.verify_wallet_session(payload)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=service.encode_session(session),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=service.should_secure_cookie(request),
        path="/",
    )
    return session


@router.post("/auth/logout")
async def public_auth_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/agents", response_model=AgentListResponse)
@router.get("/robots", response_model=AgentListResponse, include_in_schema=False)
async def public_agents(request: Request) -> AgentListResponse:
    require_session(request)
    settings = Settings()
    return AgentRegistry(settings.repo_root).list_items()


@router.get("/agent/types", response_model=AgentTypesResponse)
@router.get("/robot/types", response_model=AgentTypesResponse, include_in_schema=False)
async def public_agent_types(request: Request) -> AgentTypesResponse:
    require_session(request)
    return AgentTypesResponse(
        agentTypes=[
            {"id": "whatsapp", "name": "WhatsApp eCommerce", "requires": ["manual_pairing"]},
            {"id": "dingtalk", "name": "DingTalk", "requires": ["client_id", "client_secret"]},
        ]
    )


@router.get("/router/models", response_model=RouterModelsResponse)
async def public_router_models(request: Request) -> RouterModelsResponse:
    require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    return service.router_models()


@router.get("/agent/instances", response_model=BotInstanceListResponse)
@router.get("/robot/instances", response_model=BotInstanceListResponse, include_in_schema=False)
async def public_bot_instances(request: Request) -> BotInstanceListResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    listing = service.list_instances()
    listing.items = [item for item in listing.items if owner_matches_wallet(item.owner_wallet, session.wallet_id)]
    return listing


@router.post("/agent/instances", response_model=BotInstanceView)
@router.post("/robot/instances", response_model=BotInstanceView, include_in_schema=False)
async def public_create_bot_instance(payload: BotInstanceCreateRequest, request: Request) -> BotInstanceView:
    settings = Settings()
    service = messenger_service(settings)
    session = require_session(request)
    try:
        return service.create_instance(payload, owner_wallet=session.wallet_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/agent/instances/{instance_id}", response_model=BotInstanceView)
@router.get("/robot/instances/{instance_id}", response_model=BotInstanceView, include_in_schema=False)
async def public_bot_instance(instance_id: str, request: Request) -> BotInstanceView:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    _db, raw = require_owned_instance(service, instance_id, session)
    return service._view_from_raw(instance_id, raw)


@router.delete("/agent/instances/{instance_id}")
@router.delete("/robot/instances/{instance_id}", include_in_schema=False)
async def public_delete_bot_instance(instance_id: str, request: Request) -> dict:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.delete_instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        message = str(exc)
        status_code = 409 if "stop it before delete" in message else 502
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.patch("/agent/instances/{instance_id}/model", response_model=BotInstanceView)
@router.patch("/robot/instances/{instance_id}/model", response_model=BotInstanceView, include_in_schema=False)
async def public_patch_bot_instance_model(
    instance_id: str,
    payload: BotInstanceUpdateModelRequest,
    request: Request,
) -> BotInstanceView:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.update_instance_model(instance_id, payload.model)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agent/instances/{instance_id}/logs", response_model=BotInstanceLogsResponse)
@router.get("/robot/instances/{instance_id}/logs", response_model=BotInstanceLogsResponse, include_in_schema=False)
async def public_bot_instance_logs(
    instance_id: str,
    request: Request,
    lines: int = 120,
) -> BotInstanceLogsResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    require_owned_instance(service, instance_id, session)
    logs = service.get_instance_logs(instance_id, lines=lines)
    if logs is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return logs


@router.get("/agent/instances/{instance_id}/diagnose", response_model=BotInstanceDiagnoseResponse)
@router.get("/robot/instances/{instance_id}/diagnose", response_model=BotInstanceDiagnoseResponse, include_in_schema=False)
async def public_bot_instance_diagnose(
    instance_id: str,
    request: Request,
    auto_recover: bool = Query(default=False),
) -> BotInstanceDiagnoseResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.diagnose_instance(instance_id, auto_recover=auto_recover)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/agent/instances/{instance_id}/start", response_model=BotInstanceActionResponse)
@router.post("/robot/instances/{instance_id}/start", response_model=BotInstanceActionResponse, include_in_schema=False)
async def public_start_bot_instance(instance_id: str, request: Request) -> BotInstanceActionResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.start_instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/agent/instances/{instance_id}/stop", response_model=BotInstanceActionResponse)
@router.post("/robot/instances/{instance_id}/stop", response_model=BotInstanceActionResponse, include_in_schema=False)
async def public_stop_bot_instance(instance_id: str, request: Request) -> BotInstanceActionResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.stop_instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/agent/instances/{instance_id}/pair-whatsapp", response_model=BotInstancePairResponse)
@router.post("/robot/instances/{instance_id}/pair-whatsapp", response_model=BotInstancePairResponse, include_in_schema=False)
async def public_pair_whatsapp(instance_id: str, request: Request) -> BotInstancePairResponse:
    session = require_session(request)
    settings = Settings()
    service = messenger_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.pair_whatsapp(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/agents/{agent_key}/summary", response_model=AgentWorkspaceSummaryResponse)
@router.get("/robots/{agent_key}/summary", response_model=AgentWorkspaceSummaryResponse, include_in_schema=False)
async def public_agent_summary(agent_key: str, request: Request) -> AgentWorkspaceSummaryResponse:
    require_session(request)
    settings = Settings()
    adapter = workspace_adapter_for_agent(settings, agent_key)
    return adapter.summary()


@router.get("/agents/{agent_key}/config", response_model=AgentWorkspaceSummaryResponse)
@router.get("/robots/{agent_key}/config", response_model=AgentWorkspaceSummaryResponse, include_in_schema=False)
async def public_agent_config(agent_key: str, request: Request) -> AgentWorkspaceSummaryResponse:
    return await public_agent_summary(agent_key, request)


@router.put("/agents/{agent_key}/config", response_model=AgentWorkspaceConfigUpdateResponse)
@router.put("/robots/{agent_key}/config", response_model=AgentWorkspaceConfigUpdateResponse, include_in_schema=False)
async def public_agent_config_update(
    agent_key: str,
    payload: AgentWorkspaceConfigUpdateRequest,
    request: Request,
) -> AgentWorkspaceConfigUpdateResponse:
    require_session(request)
    settings = Settings()
    adapter = workspace_adapter_for_agent(settings, agent_key)
    if not adapter.exists():
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        return adapter.update_config(payload.broker, payload.strategy, payload.strategy_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=405, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/agents/{agent_key}/actions/run-once", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/run-once", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_run_once(agent_key: str, request: Request) -> AgentWorkspaceActionResponse:
    return await _run_agent_action(agent_key, "run_once", request)


@router.post("/agents/{agent_key}/actions/start", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/start", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_start(agent_key: str, request: Request) -> AgentWorkspaceActionResponse:
    return await _run_agent_action(agent_key, "start", request)


@router.post("/agents/{agent_key}/actions/stop", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/stop", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_stop(agent_key: str, request: Request) -> AgentWorkspaceActionResponse:
    return await _run_agent_action(agent_key, "stop", request)


@router.post("/agents/{agent_key}/actions/diagnose", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/diagnose", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_diagnose(agent_key: str, request: Request) -> AgentWorkspaceActionResponse:
    return await _run_agent_action(agent_key, "diagnose", request)


@router.post("/agents/{agent_key}/actions/init-config", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/init-config", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_init_config(agent_key: str, request: Request) -> AgentWorkspaceActionResponse:
    return await _run_agent_action(agent_key, "init_config", request)


@router.post("/agents/{agent_key}/actions/unlock-order", response_model=AgentWorkspaceActionResponse)
@router.post("/robots/{agent_key}/actions/unlock-order", response_model=AgentWorkspaceActionResponse, include_in_schema=False)
async def public_agent_unlock_order(
    agent_key: str,
    payload: AgentWorkspaceOrderUnlockRequest,
    request: Request,
) -> AgentWorkspaceActionResponse:
    require_session(request)
    settings = Settings()
    adapter = workspace_adapter_for_agent(settings, agent_key)
    if not adapter.exists():
        raise HTTPException(status_code=404, detail="agent not found")
    unlocker = getattr(adapter, "unlock_order_guard", None)
    if unlocker is None:
        raise HTTPException(status_code=405, detail="agent does not support order guard unlock")
    try:
        return unlocker(payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _run_agent_action(agent_key: str, action: str, request: Request) -> AgentWorkspaceActionResponse:
    require_session(request)
    settings = Settings()
    adapter = workspace_adapter_for_agent(settings, agent_key)
    if not adapter.exists():
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        return adapter.run_action(action)
    except NotImplementedError as exc:
        raise HTTPException(status_code=405, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
