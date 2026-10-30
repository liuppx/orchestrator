from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.adapters import AgentWorkspaceAdapter, get_agent_workspace_adapter
from orchestrator.config import Settings
from orchestrator.storage import engine_for
from orchestrator.models import (
    AuthChallengeRequest,
    AuthChallengeView,
    BotInstanceActionResponse,
    AuthSessionView,
    AuthSessionVerifyRequest,
    IdentityLoginResultEnvelope,
    IdentityLoginSessionEnvelope,
    IdentityLoginSessionView,
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
    RuntimeInstanceActionResponse,
    RuntimeInstanceCreateRequest,
    RuntimeInstanceListResponse,
    RuntimeInstanceView,
    RuntimeProviderListResponse,
    VersionResponse,
)
from orchestrator.services import (
    OpenClawRuntimeService,
    AgentRegistry,
    SESSION_COOKIE_NAME,
    auth_service,
)
from orchestrator.services.identity_auth import IdentityAuthService
from orchestrator.services.openclaw import short_wallet
from orchestrator.services.runtime_providers import runtime_providers

router = APIRouter(prefix="/api/v1/public", tags=["public"])


def identity_auth_service(settings: Settings) -> IdentityAuthService:
    return IdentityAuthService(settings, auth_service(settings))


def set_identity_session_cookie(response: Response, settings: Settings, request: Request, token: str) -> None:
    service = auth_service(settings)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=service.should_secure_cookie(request),
        path="/",
    )


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
    service: OpenClawRuntimeService,
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


def openclaw_runtime_service(settings: Settings) -> OpenClawRuntimeService:
    from orchestrator.services.openclaw import OpenClawRuntimeConfig

    return OpenClawRuntimeService(
        OpenClawRuntimeConfig(
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
            database_url=settings.database_url,
            secret_store_dir=settings.secret_store_dir or (settings.resolved_runtime_dir / "secrets"),
            node_id=settings.node_id,
        )
    )


def runtime_instance_view(instance: BotInstanceView) -> RuntimeInstanceView:
    capabilities = ["start", "stop", "logs", "diagnose"]
    if instance.kind == "whatsapp":
        capabilities.append("channel-pairing")
    return RuntimeInstanceView(
        id=instance.id,
        name=instance.name or instance.id,
        runtime_type="openclaw",
        workload_type=instance.kind,
        model=instance.model,
        desired_state=instance.desired_state,
        observed_state=instance.status,
        owner_wallet=instance.owner_wallet,
        endpoint=f"http://127.0.0.1:{instance.port}",
        process_id=instance.pid,
        last_error=instance.last_error,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        capabilities=capabilities,
    )
def workspace_adapter_for_agent(settings: Settings, agent_key: str) -> AgentWorkspaceAdapter:
    if agent_key == "messenger":
        from orchestrator.adapters.messenger import MessengerWorkspaceAdapter

        return MessengerWorkspaceAdapter(
            settings.repo_root / "agents" / "custom" / "messenger",
            settings.repo_root,
            runtime_dir=settings.resolved_runtime_dir,
            instances_root=settings.resolved_instances_root,
            database_url=settings.database_url,
            secret_store_dir=settings.secret_store_dir,
            node_id=settings.node_id,
        )
    adapter = get_agent_workspace_adapter(settings.repo_root, agent_key)
    if adapter is None:
        raise HTTPException(status_code=404, detail="agent capability not found")
    return adapter


@router.get("/health", response_model=HealthResponse)
async def public_health() -> HealthResponse:
    return HealthResponse()


@router.get("/ready")
async def public_readiness() -> dict[str, object]:
    settings = Settings()
    if not settings.database_url:
        return {"ok": True, "storage": "json"}
    try:
        with engine_for(settings.database_url).connect() as connection:
            connection.execute(text("SELECT 1"))
            schema_version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable or schema not migrated") from exc
    return {"ok": True, "storage": "database", "schema_version": schema_version}


@router.get("/version", response_model=VersionResponse)
async def public_version() -> VersionResponse:
    return VersionResponse()


@router.get("/runtime/providers", response_model=RuntimeProviderListResponse)
async def public_runtime_providers(request: Request) -> RuntimeProviderListResponse:
    require_session(request)
    return runtime_providers(Settings())


@router.get("/runtime/instances", response_model=RuntimeInstanceListResponse)
async def public_runtime_instances(request: Request) -> RuntimeInstanceListResponse:
    session = require_session(request)
    service = openclaw_runtime_service(Settings())
    instances = [
        item for item in service.list_instances().items
        if owner_matches_wallet(item.owner_wallet, session.wallet_id)
    ]
    items = [runtime_instance_view(item) for item in instances]
    return RuntimeInstanceListResponse(
        items=items,
        total=len(items),
        running=sum(1 for item in items if item.observed_state == "running"),
        stopped=sum(1 for item in items if item.observed_state in {"created", "stopped"}),
        attention=sum(1 for item in items if item.observed_state in {"error", "unknown"}),
    )


@router.post("/runtime/instances", response_model=RuntimeInstanceView)
async def public_create_runtime_instance(
    payload: RuntimeInstanceCreateRequest,
    request: Request,
) -> RuntimeInstanceView:
    if payload.runtime_type != "openclaw":
        raise HTTPException(status_code=400, detail="runtime_type is not supported")
    settings = Settings()
    session = require_session(request)
    service = openclaw_runtime_service(settings)
    try:
        instance = service.create_instance(
            BotInstanceCreateRequest(
                kind=payload.kind,
                name=payload.name,
                model=payload.model,
                template=payload.template,
                dingtalk_client_id=payload.dingtalk_client_id,
                dingtalk_client_secret=payload.dingtalk_client_secret,
            ),
            owner_wallet=session.wallet_id,
        )
        return runtime_instance_view(instance)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/runtime/instances/{instance_id}/actions/{action}", response_model=RuntimeInstanceActionResponse)
async def public_runtime_instance_action(
    instance_id: str,
    action: str,
    request: Request,
) -> RuntimeInstanceActionResponse:
    session = require_session(request)
    settings = Settings()
    service = openclaw_runtime_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        if action == "start":
            result = service.start_instance(instance_id)
        elif action == "stop":
            result = service.stop_instance(instance_id)
        else:
            raise HTTPException(status_code=404, detail="runtime action not found")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RuntimeInstanceActionResponse(
        message=result.message,
        instance=runtime_instance_view(result.instance),
    )


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


@router.post("/auth/identity/login/session", response_model=IdentityLoginSessionEnvelope)
async def public_auth_identity_login_session(request: Request, avatar: int = Query(default=1)) -> dict[str, object]:
    settings = Settings()
    service = auth_service(settings)
    base_url = service.resolve_base_url(request).rstrip("/")
    parsed = urlparse(base_url)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    try:
        return {"code": 0, "message": "ok", "data": identity_auth_service(settings).create_wallet_session(audience, avatar=avatar == 1)}
    except ValueError as exc:
        return JSONResponse(status_code=503, content={"code": 1, "message": str(exc)})


@router.post("/auth/identity/login/verify", response_model=IdentityLoginResultEnvelope)
async def public_auth_identity_login_verify(payload: dict[str, object], request: Request, response: Response) -> dict[str, object]:
    settings = Settings()
    identity = identity_auth_service(settings)
    session_id = str(payload.get("session_id", "")).strip()
    presentation = payload.get("presentation")
    if not session_id or not isinstance(presentation, dict):
        return JSONResponse(status_code=400, content={"code": 1, "message": "登录参数无效"})
    try:
        session, did = identity.verify_wallet_session(session_id, presentation)
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"code": 1, "message": str(exc)})
    token = auth_service(settings).encode_session(session)
    set_identity_session_cookie(response, settings, request, token)
    return {"code": 0, "message": "ok", "data": {
        "token": token, "did": did, "walletAddress": session.wallet_id, "wallet_address": session.wallet_id,
        "expires_at": session.expires_at, "auth_type": session.auth_type,
    }}


@router.post("/auth/passport/sessions")
async def public_auth_passport_session(request: Request) -> dict[str, object]:
    settings = Settings()
    callback = settings.identity_redirect_uri or auth_service(settings).resolve_base_url(request).rstrip("/") + "/api/v1/public/auth/passport/callback"
    try:
        return identity_auth_service(settings).create_passkey_session(callback)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/auth/passport/sessions/{session_id}")
async def public_auth_passport_status(session_id: str, request: Request, response: Response) -> dict[str, object]:
    settings = Settings()
    try:
        payload, session = identity_auth_service(settings).poll_passkey_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.get("status") == "completed" and session is not None:
        token = auth_service(settings).encode_session(session)
        set_identity_session_cookie(response, settings, request, token)
    return payload


@router.get("/auth/passport/callback")
async def public_auth_passport_callback(code: str, state: str) -> Response:
    settings = Settings()
    try:
        identity_auth_service(settings).callback(state, code)
    except ValueError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    return Response(
        content="<!doctype html><html><meta charset='utf-8'><title>登录已确认</title><body>登录已确认，请返回应用。<script>try{window.close()}catch(e){}</script></body></html>",
        media_type="text/html",
    )


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
    service = openclaw_runtime_service(settings)
    return service.router_models()


@router.get("/agent/instances", response_model=BotInstanceListResponse)
@router.get("/robot/instances", response_model=BotInstanceListResponse, include_in_schema=False)
async def public_bot_instances(request: Request) -> BotInstanceListResponse:
    session = require_session(request)
    settings = Settings()
    service = openclaw_runtime_service(settings)
    listing = service.list_instances()
    listing.items = [item for item in listing.items if owner_matches_wallet(item.owner_wallet, session.wallet_id)]
    return listing


@router.post("/agent/instances", response_model=BotInstanceView)
@router.post("/robot/instances", response_model=BotInstanceView, include_in_schema=False)
async def public_create_bot_instance(payload: BotInstanceCreateRequest, request: Request) -> BotInstanceView:
    settings = Settings()
    service = openclaw_runtime_service(settings)
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
    service = openclaw_runtime_service(settings)
    _db, raw = require_owned_instance(service, instance_id, session)
    return service._view_from_raw(instance_id, raw)


@router.delete("/agent/instances/{instance_id}")
@router.delete("/robot/instances/{instance_id}", include_in_schema=False)
async def public_delete_bot_instance(instance_id: str, request: Request) -> dict:
    session = require_session(request)
    settings = Settings()
    service = openclaw_runtime_service(settings)
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
    service = openclaw_runtime_service(settings)
    try:
        require_owned_instance(service, instance_id, session)
        return service.update_instance_model(instance_id, payload.model)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/runtime/instances/{instance_id}/logs", response_model=BotInstanceLogsResponse)
@router.get("/agent/instances/{instance_id}/logs", response_model=BotInstanceLogsResponse)
@router.get("/robot/instances/{instance_id}/logs", response_model=BotInstanceLogsResponse, include_in_schema=False)
async def public_bot_instance_logs(
    instance_id: str,
    request: Request,
    lines: int = 120,
) -> BotInstanceLogsResponse:
    session = require_session(request)
    settings = Settings()
    service = openclaw_runtime_service(settings)
    require_owned_instance(service, instance_id, session)
    logs = service.get_instance_logs(instance_id, lines=lines)
    if logs is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return logs


@router.get("/runtime/instances/{instance_id}/diagnose", response_model=BotInstanceDiagnoseResponse)
@router.get("/agent/instances/{instance_id}/diagnose", response_model=BotInstanceDiagnoseResponse)
@router.get("/robot/instances/{instance_id}/diagnose", response_model=BotInstanceDiagnoseResponse, include_in_schema=False)
async def public_bot_instance_diagnose(
    instance_id: str,
    request: Request,
    auto_recover: bool = Query(default=False),
) -> BotInstanceDiagnoseResponse:
    session = require_session(request)
    settings = Settings()
    service = openclaw_runtime_service(settings)
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
    service = openclaw_runtime_service(settings)
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
    service = openclaw_runtime_service(settings)
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
    service = openclaw_runtime_service(settings)
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
