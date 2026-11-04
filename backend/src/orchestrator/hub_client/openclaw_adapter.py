from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from orchestrator.config import Settings
from orchestrator.protocols.hub_v1 import (
    DeploymentMode,
    HubCancelRunPayloadV1,
    HubCommandOperation,
    HubCommandV1,
    HubInstanceScheduleV1,
    HubOpenClawDeletedResultV1,
    HubOpenClawDiagnosePayloadV1,
    HubOpenClawDiagnoseResultV1,
    HubOpenClawLogsPayloadV1,
    HubOpenClawLogsResultV1,
    HubOpenClawModelPayloadV1,
    HubOpenClawModelResultV1,
    HubOpenClawPairResultV1,
    HubOpenClawCreatePayloadV1,
    HubRuntimeAdapterV1,
    HubRuntimeInstanceV1,
    HubRunCancelledResultV1,
    HubRunOncePayloadV1,
    HubRunOnceResultV1,
    RuntimeObservedState,
)
from orchestrator.models import BotInstanceCreateRequest
from orchestrator.services.openclaw import (
    OpenClawCommandCancelled,
    OpenClawCommandTimeout,
    OpenClawRuntimeConfig,
    OpenClawRuntimeService,
)

from .core import AdapterExecution, AdapterExecutionContext, AdapterExecutionUncertain


MAX_LOG_SECTION_BYTES = 18 * 1024


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class OpenClawAdapter:
    """Hub-side lifecycle adapter for OpenClaw instances stored on this machine."""

    runtime_type = "openclaw"
    capabilities = [
        HubCommandOperation.CREATE_INSTANCE.value,
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.DELETE_INSTANCE.value,
        HubCommandOperation.UPDATE_INSTANCE_MODEL.value,
        HubCommandOperation.GET_INSTANCE_LOGS.value,
        HubCommandOperation.DIAGNOSE_INSTANCE.value,
        HubCommandOperation.PAIR_WHATSAPP.value,
        HubCommandOperation.RUN_INSTANCE_ONCE.value,
        HubCommandOperation.CANCEL_RUN.value,
    ]
    instance_capabilities = [
        HubCommandOperation.INSPECT_INSTANCE.value,
        HubCommandOperation.START_INSTANCE.value,
        HubCommandOperation.STOP_INSTANCE.value,
        HubCommandOperation.DELETE_INSTANCE.value,
        HubCommandOperation.UPDATE_INSTANCE_MODEL.value,
        HubCommandOperation.GET_INSTANCE_LOGS.value,
        HubCommandOperation.DIAGNOSE_INSTANCE.value,
    ]

    def __init__(self, service: OpenClawRuntimeService, secret_encryption_key: str | None = None):
        self.service = service
        self.secret_encryption_key = secret_encryption_key

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenClawAdapter":
        return cls(
            OpenClawRuntimeService(cls._config_from_settings(settings)),
            secret_encryption_key=settings.hub_command_encryption_key,
        )

    @staticmethod
    def _config_from_settings(settings: Settings, *, database_url: str | None = None) -> OpenClawRuntimeConfig:
        local_runtime_dir = settings.resolved_hub_openclaw_runtime_dir
        return OpenClawRuntimeConfig(
            state_file=settings.resolved_hub_openclaw_state_file,
            instances_root=settings.resolved_hub_openclaw_instances_root,
            runtime_dir=local_runtime_dir,
            repo_root=settings.repo_root,
            default_model=settings.default_model,
            model_allowlist=settings.parsed_model_allowlist,
            router_base_url=settings.router_base_url,
            router_api_key=settings.router_api_key,
            port_range_start=settings.instance_port_start,
            port_range_end=settings.instance_port_end,
            command_timeout_seconds=settings.hub_openclaw_command_timeout_seconds,
            openclaw_prefix=settings.openclaw_prefix,
            database_url=database_url,
            secret_store_dir=(
                settings.secret_store_dir or (settings.resolved_runtime_dir / "secrets")
                if database_url
                else settings.resolved_hub_openclaw_secret_store_dir
            ),
            node_id=settings.hub_id or settings.node_id,
        )

    @classmethod
    def migrate_backend_state_to_local(cls, settings: Settings) -> int:
        """Copy existing OpenClaw records into the Hub-local store without deleting the source."""
        if not settings.database_url:
            raise ValueError("HUB_DATABASE_URL is required to copy existing OpenClaw state")
        target_path = settings.resolved_hub_openclaw_state_file
        if target_path.exists():
            raise ValueError(f"Hub-local OpenClaw state already exists: {target_path}")

        source = OpenClawRuntimeService(cls._config_from_settings(settings, database_url=settings.database_url))
        target = OpenClawRuntimeService(cls._config_from_settings(settings))
        state = source.read_db()
        instances = state.get("instances", {})
        if not isinstance(instances, dict):
            raise ValueError("Backend OpenClaw state contains an invalid instance catalog")
        state.pop("_storage_snapshot", None)
        target.persist_db(state)
        return len(instances)

    def descriptor(self) -> HubRuntimeAdapterV1:
        return HubRuntimeAdapterV1(
            runtime_type=self.runtime_type,
            adapter_version="0.1.0",
            capabilities=self.capabilities,
        )

    def _observed_state(self, raw: dict[str, Any]) -> RuntimeObservedState:
        pid = raw.get("pid")
        running = pid is not None and self.service.is_pid_alive(int(pid))
        if not running:
            running = self.service.find_gateway_pid_for_profile(str(raw.get("profile", ""))) is not None
        if running:
            return RuntimeObservedState.RUNNING
        status = str(raw.get("status", "unknown")).lower()
        if status == "completed":
            return RuntimeObservedState.COMPLETED
        if status == "error":
            return RuntimeObservedState.ERROR
        if status in {"starting", "stopping"}:
            return RuntimeObservedState.STARTING
        if status in {"created", "stopped"}:
            return RuntimeObservedState.STOPPED
        return RuntimeObservedState.UNKNOWN

    def discover(self) -> list[HubRuntimeInstanceV1]:
        instances: list[HubRuntimeInstanceV1] = []
        for view in self.service.list_instances().items:
            record = self.service.get_instance_raw(view.id)
            if record is None:
                continue
            _db, raw = record
            deployment_mode = DeploymentMode(raw.get("deployment_mode") or "service")
            capabilities = list(self.instance_capabilities)
            if view.kind == "whatsapp":
                capabilities.append(HubCommandOperation.PAIR_WHATSAPP.value)
            if deployment_mode == DeploymentMode.SCHEDULED:
                # For scheduled instances start/stop enable/disable the schedule on this Hub
                # (nothing is launched); run_instance_once / cancel_run are the real work.
                capabilities.extend([HubCommandOperation.RUN_INSTANCE_ONCE.value, HubCommandOperation.CANCEL_RUN.value])
            instances.append(
                HubRuntimeInstanceV1(
                    instance_id=view.id,
                    runtime_type=self.runtime_type,
                    display_name=view.name[:128],
                    workload_type=view.kind,
                    model=view.model[:128],
                    port=view.port,
                    process_id=view.pid,
                    observed_state=self._observed_state(raw),
                    capabilities=capabilities,
                    observed_at=datetime.now(UTC),
                    deployment_mode=deployment_mode,
                    schedule=HubInstanceScheduleV1.model_validate(raw["schedule"]) if raw.get("schedule") else None,
                )
            )
        return instances

    def execute(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        try:
            return self._execute(command, context)
        except OpenClawCommandTimeout as exc:
            raise AdapterExecutionUncertain("COMMAND_TIMEOUT") from exc
        except OpenClawCommandCancelled as exc:
            raise AdapterExecutionUncertain("COMMAND_CANCELLED") from exc

    def _execute(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        context.raise_if_cancelled()
        cancellation_requested = lambda: context.cancellation_requested
        if command.operation == HubCommandOperation.CREATE_INSTANCE:
            return self._create_instance(command, context)
        if command.operation == HubCommandOperation.INSPECT_INSTANCE:
            record = self.service.get_instance_raw(command.target_instance_id)
            if record is None:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            return AdapterExecution(True, self._observed_state(record[1]), progress_percent=100)
        if command.operation == HubCommandOperation.DELETE_INSTANCE:
            try:
                self.service.delete_instance(command.target_instance_id)
            except KeyError:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            except RuntimeError as exc:
                if "stop it before delete" in str(exc):
                    return AdapterExecution(False, RuntimeObservedState.RUNNING, "INSTANCE_RUNNING")
                raise
            return AdapterExecution(
                True,
                RuntimeObservedState.NOT_INSTALLED,
                progress_percent=100,
                result=HubOpenClawDeletedResultV1(
                    kind="instance_deleted",
                    instance_id=command.target_instance_id,
                    archived={},
                ),
            )
        if command.operation == HubCommandOperation.UPDATE_INSTANCE_MODEL:
            payload = command.operation_payload
            if not isinstance(payload, HubOpenClawModelPayloadV1):
                return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
            try:
                updated = self.service.update_instance_model(
                    command.target_instance_id,
                    payload.model,
                    cancellation_requested=cancellation_requested,
                )
            except KeyError:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            return AdapterExecution(
                True,
                RuntimeObservedState(updated.status) if updated.status in RuntimeObservedState._value2member_map_ else RuntimeObservedState.UNKNOWN,
                progress_percent=100,
                result=HubOpenClawModelResultV1(kind="model_updated", model=updated.model),
            )
        if command.operation == HubCommandOperation.GET_INSTANCE_LOGS:
            payload = command.operation_payload
            if not isinstance(payload, HubOpenClawLogsPayloadV1):
                return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
            logs = self.service.get_instance_logs(command.target_instance_id, lines=payload.lines)
            if logs is None:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            raw_record = self.service.get_instance_raw(command.target_instance_id)
            raw = raw_record[1] if raw_record is not None else {}
            return AdapterExecution(
                True,
                self._observed_state(raw) if raw else RuntimeObservedState.UNKNOWN,
                progress_percent=100,
                result=HubOpenClawLogsResultV1(
                    kind="instance_logs",
                    id=logs.id,
                    gateway_log=self._safe_log_text(logs.gateway_log, raw),
                    pair_log=self._safe_log_text(logs.pair_log, raw),
                    pair_qr_ascii=_truncate_utf8(self._safe_log_text(logs.pair_qr_ascii, raw), 8192),
                    pair_status=logs.pair_status[:32],
                    pair_hint=self._safe_log_text(logs.pair_hint or "", raw)[:512] or None,
                    gateway_log_path="logs/gateway.log",
                    pair_log_path="logs/pair.log",
                    events_log_path="logs/events.jsonl",
                    events_log=self._safe_log_text(logs.events_log, raw),
                ),
            )
        if command.operation == HubCommandOperation.DIAGNOSE_INSTANCE:
            payload = command.operation_payload
            if not isinstance(payload, HubOpenClawDiagnosePayloadV1):
                return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
            try:
                diagnosis = self.service.diagnose_instance(
                    command.target_instance_id,
                    auto_recover=payload.auto_recover,
                    cancellation_requested=cancellation_requested,
                )
            except KeyError:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            raw_record = self.service.get_instance_raw(command.target_instance_id)
            raw = raw_record[1] if raw_record is not None else {}
            result = HubOpenClawDiagnoseResultV1(
                kind="instance_diagnosis",
                id=diagnosis.id,
                profile=diagnosis.profile,
                workload_type=diagnosis.kind,
                status=diagnosis.status,
                port=diagnosis.port,
                pid=diagnosis.pid,
                gateway_target=self._safe_log_text(diagnosis.gateway_target, raw)[:256],
                gateway_reachable=diagnosis.gateway_reachable,
                pair_status=diagnosis.pair_status,
                pair_hint=self._safe_log_text(diagnosis.pair_hint or "", raw)[:512] or None,
                whatsapp_running=diagnosis.whatsapp_running,
                whatsapp_connected=diagnosis.whatsapp_connected,
                whatsapp_last_error=(
                    self._safe_log_text(diagnosis.whatsapp_last_error, raw)[:1024]
                    if diagnosis.whatsapp_last_error
                    else None
                ),
                last_inbound_at=diagnosis.last_inbound_at,
                last_outbound_at=diagnosis.last_outbound_at,
                transport_established=diagnosis.transport_established,
                router_api_key_present=diagnosis.router_api_key_present,
                no_api_key_error_seen=diagnosis.no_api_key_error_seen,
                recommended_action=(
                    self._safe_log_text(diagnosis.recommended_action, raw)[:256]
                    if diagnosis.recommended_action
                    else None
                ),
                auto_recover_triggered=diagnosis.auto_recover_triggered,
                auto_recover_message=(
                    self._safe_log_text(diagnosis.auto_recover_message, raw)[:512]
                    if diagnosis.auto_recover_message
                    else None
                ),
                evidence=[self._safe_log_text(item, raw)[:1024] for item in diagnosis.evidence[:16]],
            )
            return AdapterExecution(
                True,
                RuntimeObservedState(diagnosis.status)
                if diagnosis.status in RuntimeObservedState._value2member_map_
                else RuntimeObservedState.UNKNOWN,
                progress_percent=100,
                result=result,
            )
        if command.operation == HubCommandOperation.PAIR_WHATSAPP:
            try:
                pairing = self.service.pair_whatsapp(
                    command.target_instance_id,
                    cancellation_requested=cancellation_requested,
                )
            except KeyError:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            record = self.service.get_instance_raw(command.target_instance_id)
            observed_state = self._observed_state(record[1]) if record is not None else RuntimeObservedState.UNKNOWN
            return AdapterExecution(
                True,
                observed_state,
                progress_percent=100,
                result=HubOpenClawPairResultV1(
                    kind="whatsapp_pair_started",
                    id=command.target_instance_id,
                    message=pairing.message,
                    pair_pid=pairing.pair_pid,
                    pair_log="logs/pair.log",
                ),
            )
        if command.operation == HubCommandOperation.RUN_INSTANCE_ONCE:
            return self._run_once(command, context)
        if command.operation == HubCommandOperation.CANCEL_RUN:
            payload = command.operation_payload
            if not isinstance(payload, HubCancelRunPayloadV1):
                return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
            record = self.service.get_instance_raw(command.target_instance_id)
            if record is None:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            # This adapter executes serially: by the time a cancel command runs, no run is in flight.
            return AdapterExecution(
                True,
                self._observed_state(record[1]),
                progress_percent=100,
                result=HubRunCancelledResultV1(kind="run_cancelled", run_id=payload.run_id, state="already_finished"),
            )
        if command.operation == HubCommandOperation.START_INSTANCE:
            self.service.start_instance(
                command.target_instance_id,
                cancellation_requested=cancellation_requested,
            )
            expected_state = RuntimeObservedState.RUNNING
        elif command.operation == HubCommandOperation.STOP_INSTANCE:
            self.service.stop_instance(
                command.target_instance_id,
                cancellation_requested=cancellation_requested,
            )
            expected_state = RuntimeObservedState.STOPPED
        else:
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "UNSUPPORTED")
        record = self.service.get_instance_raw(command.target_instance_id)
        if record is not None and record[1].get("deployment_mode") == "scheduled":
            # Enabling/disabling a schedule launches nothing, so there is no state to converge on.
            return AdapterExecution(True, self._observed_state(record[1]), progress_percent=100)
        deadline = time.monotonic() + 5
        state = RuntimeObservedState.UNKNOWN
        while time.monotonic() < deadline:
            context.raise_if_cancelled()
            record = self.service.get_instance_raw(command.target_instance_id)
            state = self._observed_state(record[1]) if record is not None else RuntimeObservedState.UNKNOWN
            if state == expected_state:
                return AdapterExecution(True, state, progress_percent=100)
            context.wait_for_cancellation(0.1)
        return AdapterExecution(False, state, "STATE_NOT_CONVERGED")

    # Stage-B placeholder workload: a bounded, side-effect-free diagnostic probe that
    # exercises the full scheduling pipeline. It is NOT an agent task; real CLI
    # workloads arrive with the Codex / Claude Code adapters.
    RUN_ONCE_WORKLOAD = "diagnose"

    def schedule_enabled(self, instance_id: str) -> bool:
        """A schedule only fires once its instance has been explicitly enabled (desired_state=running)."""
        record = self.service.get_instance_raw(instance_id)
        if record is None:
            return False
        raw = record[1]
        return raw.get("deployment_mode") == "scheduled" and raw.get("desired_state") == "running"

    @staticmethod
    def _write_run_log(path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _mark_run_outcome(self, instance_id: str, status: str, payload: HubRunOncePayloadV1, state: str) -> None:
        # Re-read after the workload: the service may have refreshed the record meanwhile.
        record = self.service.get_instance_raw(instance_id)
        if record is None:
            return
        db, raw = record
        raw["status"] = status
        raw["updated_at"] = datetime.now(UTC).isoformat()
        self.service.write_instance_event(
            raw,
            "run_once_finished",
            {"run_id": str(payload.run_id), "trigger": payload.trigger, "state": state, "workload": self.RUN_ONCE_WORKLOAD},
        )
        self.service.persist_db(db)

    def _run_once(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        payload = command.operation_payload
        if not isinstance(payload, HubRunOncePayloadV1):
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_OPERATION_PAYLOAD")
        record = self.service.get_instance_raw(command.target_instance_id)
        if record is None:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
        _db, raw = record
        if raw.get("deployment_mode") != "scheduled":
            return AdapterExecution(False, self._observed_state(raw), "RUN_ONCE_REQUIRES_SCHEDULED_INSTANCE")
        context.raise_if_cancelled()
        started_at = datetime.now(UTC)
        log_name = f"{payload.run_id}.log"
        log_ref = f"logs/runs/{log_name}"
        log_path = Path(str(raw.get("logs_dir", ""))) / "runs" / log_name
        header = [
            f"run_id={payload.run_id}",
            f"trigger={payload.trigger}",
            f"scheduled_for={payload.scheduled_for.isoformat()}",
            f"workload={self.RUN_ONCE_WORKLOAD}",
        ]
        try:
            diagnosis = self.service.diagnose_instance(
                command.target_instance_id,
                auto_recover=False,
                cancellation_requested=lambda: context.cancellation_requested,
            )
        except KeyError:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
        except RuntimeError as exc:
            finished_at = datetime.now(UTC)
            summary = self._safe_log_text(str(exc), raw)[:2048]
            self._write_run_log(log_path, [*header, "state=failed", f"error={summary}"])
            self._mark_run_outcome(command.target_instance_id, "error", payload, "failed")
            return AdapterExecution(
                False,
                RuntimeObservedState.ERROR,
                "RUN_WORKLOAD_FAILED",
                result=HubRunOnceResultV1(
                    kind="instance_run",
                    run_id=payload.run_id,
                    state="failed",
                    exit_code=1,
                    started_at=started_at,
                    finished_at=finished_at,
                    log_ref=log_ref,
                    summary=summary,
                ),
            )
        finished_at = datetime.now(UTC)
        self._write_run_log(
            log_path,
            [
                *header,
                "state=succeeded",
                f"status={diagnosis.status}",
                f"gateway_reachable={diagnosis.gateway_reachable}",
                *[f"evidence={self._safe_log_text(item, raw)}" for item in diagnosis.evidence[:16]],
            ],
        )
        summary = self._safe_log_text(
            f"{self.RUN_ONCE_WORKLOAD}: status={diagnosis.status} gateway_reachable={diagnosis.gateway_reachable}", raw
        )[:2048]
        self._mark_run_outcome(command.target_instance_id, "completed", payload, "succeeded")
        return AdapterExecution(
            True,
            RuntimeObservedState.COMPLETED,
            progress_percent=100,
            result=HubRunOnceResultV1(
                kind="instance_run",
                run_id=payload.run_id,
                state="succeeded",
                exit_code=0,
                started_at=started_at,
                finished_at=finished_at,
                log_ref=log_ref,
                summary=summary,
            ),
        )

    def _create_instance(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution:
        payload = command.create_payload
        if payload is None:
            return AdapterExecution(False, RuntimeObservedState.UNSUPPORTED, "INVALID_CREATE_PAYLOAD")
        existing = self.service.get_instance_raw(command.target_instance_id)
        if existing is not None:
            return AdapterExecution(True, self._observed_state(existing[1]), progress_percent=100)
        secret = None
        if payload.dingtalk_client_secret_envelope:
            if not self.secret_encryption_key:
                return AdapterExecution(False, RuntimeObservedState.ERROR, "SECRET_DECRYPTION_UNAVAILABLE")
            try:
                secret = Fernet(self.secret_encryption_key.encode("ascii")).decrypt(
                    payload.dingtalk_client_secret_envelope.encode("ascii")
                ).decode("utf-8")
            except (ValueError, UnicodeError, InvalidToken):
                return AdapterExecution(False, RuntimeObservedState.ERROR, "SECRET_DECRYPTION_FAILED")
        context.raise_if_cancelled()
        self.service.create_instance(
            BotInstanceCreateRequest(
                kind=payload.kind,
                name=payload.name,
                model=payload.model,
                template=payload.template,
                dingtalk_client_id=payload.dingtalk_client_id,
                dingtalk_client_secret=secret,
                deployment_mode=payload.deployment_mode.value,
                schedule=payload.schedule.model_dump(mode="json") if payload.schedule else None,
            ),
            owner_wallet="machine-hub",
            instance_id=command.target_instance_id,
        )
        return AdapterExecution(True, RuntimeObservedState.STOPPED, progress_percent=100)

    def _safe_log_text(self, value: str | None, raw: dict[str, Any]) -> str:
        result = self.service.strip_ansi_sequences(value or "")
        secrets = [
            self.service.cfg.router_api_key,
            raw.get("dingtalk_client_secret"),
            raw.get("dingtalk_client_id"),
        ]
        for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
            result = result.replace(secret, "[REDACTED]")
        for path_key in ("root_dir", "logs_dir"):
            path_value = raw.get(path_key)
            if path_value:
                result = result.replace(str(path_value), "<instance>")
        result = re.sub(
            r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+",
            r"\1[REDACTED]",
            result,
        )
        result = re.sub(
            r"(?i)((?:client[_-]?secret|api[_-]?key|access[_-]?token|password)\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            result,
        )
        return _truncate_utf8(result, MAX_LOG_SECTION_BYTES)

    def recover(self, command: HubCommandV1, context: AdapterExecutionContext) -> AdapterExecution | None:
        context.raise_if_cancelled()
        record = self.service.get_instance_raw(command.target_instance_id)
        if command.operation == HubCommandOperation.PAIR_WHATSAPP:
            return None
        if command.operation == HubCommandOperation.DELETE_INSTANCE:
            if record is None:
                return AdapterExecution(
                    True,
                    RuntimeObservedState.NOT_INSTALLED,
                    progress_percent=100,
                    result=HubOpenClawDeletedResultV1(
                        kind="instance_deleted",
                        instance_id=command.target_instance_id,
                        archived={},
                    ),
                )
            return self.execute(command, context)
        if command.operation == HubCommandOperation.UPDATE_INSTANCE_MODEL:
            payload = command.operation_payload
            if record is None:
                return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
            if isinstance(payload, HubOpenClawModelPayloadV1) and record[1].get("model") == payload.model:
                return AdapterExecution(
                    True,
                    self._observed_state(record[1]),
                    progress_percent=100,
                    result=HubOpenClawModelResultV1(kind="model_updated", model=payload.model),
                )
            return self.execute(command, context)
        if command.operation in {
            HubCommandOperation.GET_INSTANCE_LOGS,
            HubCommandOperation.DIAGNOSE_INSTANCE,
        }:
            return self.execute(command, context)
        if command.operation == HubCommandOperation.CREATE_INSTANCE:
            if record is not None:
                return AdapterExecution(True, self._observed_state(record[1]), progress_percent=100)
            return self.execute(command, context)
        if record is None:
            return AdapterExecution(False, RuntimeObservedState.NOT_INSTALLED, "INSTANCE_NOT_FOUND")
        state = self._observed_state(record[1])
        if command.operation == HubCommandOperation.INSPECT_INSTANCE:
            return AdapterExecution(True, state, progress_percent=100)
        if command.operation == HubCommandOperation.START_INSTANCE and state == RuntimeObservedState.RUNNING:
            return AdapterExecution(True, state, progress_percent=100)
        if command.operation == HubCommandOperation.STOP_INSTANCE and state == RuntimeObservedState.STOPPED:
            return AdapterExecution(True, state, progress_percent=100)
        if command.operation in {HubCommandOperation.START_INSTANCE, HubCommandOperation.STOP_INSTANCE}:
            return self.execute(command, context)
        return None
