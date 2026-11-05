from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.config import Settings
from orchestrator.models import RuntimeNodeHeartbeatRequest, RuntimeNodeListResponse
from orchestrator.protocols.hub_v1 import (
    HubCommandIssueRequestV1,
    HubCommandLeaseV1,
    HubCommandLeaseRenewalRequestV1,
    HubCommandOperation,
    HubCommandPollResponseV1,
    HubCreatePayloadV1,
    HubCredentialIssueRequestV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubInventorySnapshotV1,
    InstanceId,
    RuntimeType,
)
from orchestrator.services.nodes import NODE_ID_PATTERN, RuntimeNodeService
from orchestrator.services.hubs import (
    HubAuthenticationError,
    HubConflictError,
    HubNotFoundError,
    HubServiceError,
    MachineHubService,
)
from orchestrator.services.runtime import AgentRuntimeState

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])
HUB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


def machine_hub_service(settings: Settings) -> MachineHubService:
    try:
        return MachineHubService(settings.database_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def authenticate_machine_hub(
    service: MachineHubService,
    hub_id: str,
    authorization: str | None,
) -> None:
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    provided = authorization[len("Bearer ") :].strip() if authorization and authorization.startswith("Bearer ") else ""
    try:
        service.authenticate(hub_id, provided)
    except HubAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def map_hub_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HubNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, HubConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


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


@router.post("/hubs/{hub_id}/credentials")
async def internal_issue_hub_credential(
    hub_id: str,
    payload: HubCredentialIssueRequestV1,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    try:
        credential = machine_hub_service(settings).issue_credential(hub_id, payload.expires_in_seconds)
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return credential.model_dump(mode="json")


@router.post("/hubs/{hub_id}/credentials/{credential_id}/revoke")
async def internal_revoke_hub_credential(
    hub_id: str,
    credential_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    try:
        machine_hub_service(settings).revoke_credential(hub_id, credential_id)
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc
    return {"credential_id": credential_id, "status": "revoked"}


@router.get("/hubs/{hub_id}/credentials")
async def internal_list_hub_credentials(
    hub_id: str,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    try:
        credentials = machine_hub_service(settings).list_credentials(hub_id)
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc
    return {"items": [item.model_dump(mode="json") for item in credentials]}


@router.post("/hubs/{hub_id}/heartbeat")
async def internal_hub_heartbeat(
    hub_id: str,
    payload: HubHeartbeatRequestV1,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    service = machine_hub_service(settings)
    authenticate_machine_hub(service, hub_id, authorization)
    try:
        return service.heartbeat(hub_id, payload).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


@router.put("/hubs/{hub_id}/inventory")
async def internal_hub_inventory(
    hub_id: str,
    payload: HubInventorySnapshotV1,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    service = machine_hub_service(settings)
    authenticate_machine_hub(service, hub_id, authorization)
    try:
        return service.update_inventory(hub_id, payload).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


@router.post("/hubs/{hub_id}/commands", status_code=202)
async def internal_issue_hub_command(
    hub_id: str,
    payload: HubCommandIssueRequestV1,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    try:
        return machine_hub_service(settings).issue_command(hub_id, payload).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


@router.get("/hubs/{hub_id}/commands/{command_id}")
async def internal_get_hub_command_status(
    hub_id: str,
    command_id: UUID,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    require_admin_token(settings, authorization)
    if not HUB_ID_PATTERN.fullmatch(hub_id):
        raise HTTPException(status_code=404, detail="Hub not found")
    try:
        status = machine_hub_service(settings).command_status_summary(hub_id, str(command_id))
    except RuntimeError as exc:
        raise map_hub_service_error(exc) from exc
    if status is None:
        raise HTTPException(status_code=404, detail="Hub command not found")
    return status


@router.get("/hubs/{hub_id}/commands", response_model=HubCommandPollResponseV1)
async def internal_poll_hub_commands(
    hub_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    service = machine_hub_service(settings)
    authenticate_machine_hub(service, hub_id, authorization)
    try:
        return service.claim_commands(hub_id, limit).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


@router.post("/hubs/{hub_id}/commands/{command_id}/lease", response_model=HubCommandLeaseV1)
async def internal_renew_hub_command_lease(
    hub_id: str,
    command_id: UUID,
    payload: HubCommandLeaseRenewalRequestV1,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    service = machine_hub_service(settings)
    authenticate_machine_hub(service, hub_id, authorization)
    try:
        return service.renew_command_lease(hub_id, str(command_id), payload.attempt).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


@router.post("/hubs/{hub_id}/events")
async def internal_hub_events(
    hub_id: str,
    payload: HubEventBatchRequestV1,
    authorization: str | None = Header(default=None),
) -> dict:
    settings = Settings()
    service = machine_hub_service(settings)
    authenticate_machine_hub(service, hub_id, authorization)
    try:
        return service.accept_events(hub_id, payload).model_dump(mode="json")
    except (HubNotFoundError, HubConflictError, RuntimeError) as exc:
        raise map_hub_service_error(exc) from exc


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


class InternalRuntimeInstanceCreate(BaseModel):
    """Worker request to turn a verified marketplace artifact into a Hub-managed instance."""

    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, max_length=255)
    runtime_type: RuntimeType
    instance_id: InstanceId
    create_payload: HubCreatePayloadV1


@router.post("/runtime/instances")
async def internal_create_runtime_instance(
    request: Request,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
) -> dict:
    """Create a catalog instance on the configured Hub for the lifecycle worker.

    Idempotent on instance_id: a retried task finds the existing instance and does
    not issue a second create command.
    """
    from orchestrator.api.public import _await_hub_command

    settings = Settings()
    require_worker_token(settings, authorization)
    if not settings.hub_id or not settings.database_url:
        raise HTTPException(status_code=503, detail="Machine Hub is not configured for this Backend")
    try:
        body = InternalRuntimeInstanceCreate.model_validate(await request_payload(request))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    hub_service = machine_hub_service(settings)
    try:
        existing = hub_service.catalog_instance(body.instance_id)
        if existing is not None:
            if existing.get("owner_id") != body.owner_id or existing.get("runtime_type") != body.runtime_type:
                raise HTTPException(status_code=409, detail="instance id belongs to another owner or runtime")
            return ok({"instance": existing, "created": False})
        if not hub_service.is_online(settings.hub_id):
            raise HTTPException(status_code=503, detail="Machine Hub is offline")
        command = HubCommandIssueRequestV1(
            target_instance_id=body.instance_id,
            runtime_type=body.runtime_type,
            operation=HubCommandOperation.CREATE_INSTANCE,
            idempotency_key=f"worker-create-{uuid4().hex}",
            desired_revision=1,
            trace_id=f"worker-create-{uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
            create_payload=body.create_payload,
        )
        receipt = hub_service.issue_command(
            settings.hub_id,
            command,
            actor=f"worker:{str(x_worker_id or '').strip() or 'unknown'}",
            catalog_owner_id=body.owner_id,
        )
    except HubServiceError as exc:
        raise map_hub_service_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _await_hub_command(hub_service, settings.hub_id, str(receipt.command_id), "create")
    try:
        created = hub_service.catalog_instance(body.instance_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if created is None:
        raise HTTPException(status_code=502, detail="Hub created the instance but the catalog has no record of it")
    return ok({"instance": created, "created": True})


@router.post("/runtime/instances/{instance_id}/delete")
async def internal_delete_runtime_instance(
    instance_id: str,
    authorization: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None),
) -> dict:
    from orchestrator.api.public import _await_hub_command

    settings = Settings()
    require_worker_token(settings, authorization)
    if not settings.database_url:
        raise HTTPException(status_code=503, detail="Machine Hub catalog is not configured for this Backend")
    hub_service = machine_hub_service(settings)
    try:
        existing = hub_service.catalog_instance(instance_id)
        if existing is None:
            return ok({"deleted": False, "reason": "INSTANCE_NOT_FOUND"})
        hub_id = str(existing["hub_id"])
        if not hub_service.is_online(hub_id):
            raise HTTPException(status_code=503, detail="Machine Hub is offline")
        command = HubCommandIssueRequestV1(
            target_instance_id=instance_id,
            runtime_type=str(existing["runtime_type"]),
            operation=HubCommandOperation.DELETE_INSTANCE,
            idempotency_key=f"worker-delete-{uuid4().hex}",
            desired_revision=hub_service.next_desired_revision(hub_id, instance_id),
            trace_id=f"worker-delete-{uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        receipt = hub_service.issue_command(hub_id, command, actor=f"worker:{str(x_worker_id or '').strip() or 'unknown'}")
    except HubServiceError as exc:
        raise map_hub_service_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _await_hub_command(hub_service, hub_id, str(receipt.command_id), "delete", timeout_seconds=60)
    return ok({"deleted": True})


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
