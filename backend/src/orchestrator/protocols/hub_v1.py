from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


HubId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
InstanceId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
RuntimeType = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")]
Capability = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")]
ResourceName = Annotated[str, Field(min_length=1, max_length=64)]
ResourceText = Annotated[str, Field(max_length=128)]
ResourceValue = ResourceText | int | float | bool | None


class HubProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeObservedState(StrEnum):
    UNKNOWN = "unknown"
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    BUSY = "busy"
    DEGRADED = "degraded"
    ERROR = "error"
    UNSUPPORTED = "unsupported"
    # A run-once execution finished and exited; only meaningful for scheduled instances.
    COMPLETED = "completed"


class HubCommandOperation(StrEnum):
    CREATE_INSTANCE = "create_instance"
    INSPECT_INSTANCE = "inspect_instance"
    START_INSTANCE = "start_instance"
    STOP_INSTANCE = "stop_instance"
    RESTART_INSTANCE = "restart_instance"
    DELETE_INSTANCE = "delete_instance"
    UPDATE_INSTANCE_MODEL = "update_instance_model"
    GET_INSTANCE_LOGS = "get_instance_logs"
    DIAGNOSE_INSTANCE = "diagnose_instance"
    PAIR_WHATSAPP = "pair_whatsapp"
    # Additive since the deployment-mode revision; Hubs advertise them as capabilities.
    RUN_INSTANCE_ONCE = "run_instance_once"
    CANCEL_RUN = "cancel_run"


RunTrigger = Literal["schedule", "manual"]
RunState = Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
RunLogRef = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^logs/runs/[A-Za-z0-9._-]+$")]


class HubCommandPhase(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class HubRuntimeAdapterV1(HubProtocolModel):
    runtime_type: RuntimeType
    adapter_version: str = Field(min_length=1, max_length=128)
    runtime_version: str | None = Field(default=None, max_length=128)
    capabilities: list[Capability] = Field(default_factory=list, max_length=64)

    @field_validator("capabilities")
    @classmethod
    def validate_unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return value


class HubHeartbeatRequestV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    hub_version: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=255)
    operating_system: str = Field(min_length=1, max_length=64)
    operating_system_version: str | None = Field(default=None, max_length=128)
    architecture: str = Field(min_length=1, max_length=64)
    resources: dict[ResourceName, ResourceValue] = Field(default_factory=dict, max_length=64)
    adapters: list[HubRuntimeAdapterV1] = Field(default_factory=list, max_length=64)

    @field_validator("adapters")
    @classmethod
    def validate_unique_adapters(cls, value: list[HubRuntimeAdapterV1]) -> list[HubRuntimeAdapterV1]:
        runtime_types = [adapter.runtime_type for adapter in value]
        if len(runtime_types) != len(set(runtime_types)):
            raise ValueError("runtime adapter types must be unique")
        return value


class HubHeartbeatResponseV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    server_time: AwareDatetime
    heartbeat_interval_seconds: int = Field(ge=5, le=300)
    command_poll_interval_seconds: int = Field(ge=1, le=300)


class DeploymentMode(StrEnum):
    SERVICE = "service"
    SCHEDULED = "scheduled"


class HubInstanceScheduleV1(HubProtocolModel):
    """Schedule definition a Hub reports for a `scheduled` instance.

    Structural only: cron semantics are validated at the public API edge, and
    actual triggering is a Hub-local responsibility (not part of this revision).
    """

    kind: Literal["cron", "interval"]
    expr: str | None = Field(default=None, min_length=1, max_length=128)
    seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "HubInstanceScheduleV1":
        if self.kind == "cron" and (self.expr is None or self.seconds is not None):
            raise ValueError("cron schedule requires expr and must not set seconds")
        if self.kind == "interval" and (self.seconds is None or self.expr is not None):
            raise ValueError("interval schedule requires seconds and must not set expr")
        return self


class HubRuntimeInstanceV1(HubProtocolModel):
    instance_id: InstanceId
    runtime_type: RuntimeType
    display_name: str | None = Field(default=None, max_length=128)
    workload_type: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    port: int | None = Field(default=None, ge=1, le=65535)
    process_id: int | None = Field(default=None, ge=1)
    runtime_version: str | None = Field(default=None, max_length=128)
    observed_state: RuntimeObservedState
    capabilities: list[Capability] = Field(default_factory=list, max_length=64)
    observed_at: AwareDatetime
    # Additive in 1.0: Hubs that predate deployment modes simply omit these.
    deployment_mode: DeploymentMode | None = None
    schedule: HubInstanceScheduleV1 | None = None

    @model_validator(mode="after")
    def validate_schedule_matches_mode(self) -> "HubRuntimeInstanceV1":
        if self.deployment_mode == DeploymentMode.SCHEDULED and self.schedule is None:
            raise ValueError("scheduled instances must report a schedule")
        if self.deployment_mode == DeploymentMode.SERVICE and self.schedule is not None:
            raise ValueError("service instances must not report a schedule")
        return self

    @field_validator("capabilities")
    @classmethod
    def validate_unique_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return value


class HubInventorySnapshotV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    revision: int = Field(ge=1)
    captured_at: AwareDatetime
    instances: list[HubRuntimeInstanceV1] = Field(max_length=4096)

    @field_validator("instances")
    @classmethod
    def validate_unique_instances(cls, value: list[HubRuntimeInstanceV1]) -> list[HubRuntimeInstanceV1]:
        ids = [instance.instance_id for instance in value]
        if len(ids) != len(set(ids)):
            raise ValueError("instance ids must be unique within an inventory snapshot")
        return value


class HubInventoryAckV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    accepted_revision: int = Field(ge=1)
    accepted_at: AwareDatetime


class HubOpenClawCreatePayloadV1(HubProtocolModel):
    kind: Literal["whatsapp", "dingtalk"]
    name: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    template: str | None = Field(default=None, max_length=128)
    dingtalk_client_id: str | None = Field(default=None, max_length=512)
    dingtalk_client_secret_envelope: str | None = Field(default=None, max_length=4096)
    deployment_mode: DeploymentMode = DeploymentMode.SERVICE
    schedule: HubInstanceScheduleV1 | None = None

    @model_validator(mode="after")
    def validate_credentials(self) -> HubOpenClawCreatePayloadV1:
        if self.kind == "dingtalk":
            if not self.dingtalk_client_id or not self.dingtalk_client_secret_envelope:
                raise ValueError("DingTalk creation requires an encrypted client secret")
        elif self.dingtalk_client_id or self.dingtalk_client_secret_envelope:
            raise ValueError("DingTalk credentials are only valid for the DingTalk workload")
        if self.deployment_mode == DeploymentMode.SCHEDULED and self.schedule is None:
            raise ValueError("scheduled instances require a schedule")
        if self.deployment_mode == DeploymentMode.SERVICE and self.schedule is not None:
            raise ValueError("service instances must not define a schedule")
        return self


class HubOpenClawModelPayloadV1(HubProtocolModel):
    kind: Literal["update_model"]
    model: str = Field(min_length=1, max_length=128)


CLI_RUNTIME_TYPES = frozenset({"codex", "claude-code"})
CodexSandboxMode = Literal["read-only", "workspace-write"]
ClaudePermissionMode = Literal["plan", "acceptEdits", "dontAsk"]


class HubCliTaskCreatePayloadV1(HubProtocolModel):
    """Create a one-shot CLI agent task instance (Codex CLI or Claude Code CLI).

    The prompt is the instance definition — the fixed task every run executes —
    never per-run input. Execution policy is part of the definition too, and the
    bypass modes of either CLI are deliberately not representable here. A CLI
    task has no resident form, so it is always a scheduled instance.
    """

    kind: Literal["cli_task"]
    name: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=8192)
    model: str | None = Field(default=None, max_length=128)
    codex_sandbox: CodexSandboxMode | None = None
    claude_permission_mode: ClaudePermissionMode | None = None
    run_timeout_seconds: int = Field(default=900, ge=30, le=86_400)
    deployment_mode: DeploymentMode = DeploymentMode.SCHEDULED
    schedule: HubInstanceScheduleV1 | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> HubCliTaskCreatePayloadV1:
        if self.deployment_mode != DeploymentMode.SCHEDULED:
            raise ValueError("CLI task instances are always scheduled; they have no resident service form")
        if self.schedule is None:
            raise ValueError("scheduled instances require a schedule")
        if self.codex_sandbox is not None and self.claude_permission_mode is not None:
            raise ValueError("set the execution policy for exactly one CLI runtime")
        return self


HubCreatePayloadV1 = Annotated[
    Union[HubOpenClawCreatePayloadV1, HubCliTaskCreatePayloadV1],
    Field(discriminator="kind"),
]


class HubOpenClawLogsPayloadV1(HubProtocolModel):
    kind: Literal["get_logs"]
    lines: int = Field(default=120, ge=1, le=1000)


class HubOpenClawDiagnosePayloadV1(HubProtocolModel):
    kind: Literal["diagnose"]
    auto_recover: bool = False


class HubRunOncePayloadV1(HubProtocolModel):
    """Execute the instance's configured workload exactly once and exit.

    Deliberately carries no command line, arguments or environment: what runs is
    fixed by the instance definition on the Hub, never by the payload.
    """

    kind: Literal["run_once"]
    run_id: UUID
    trigger: RunTrigger
    scheduled_for: AwareDatetime
    timeout_seconds: int = Field(default=900, ge=30, le=86_400)


class HubCancelRunPayloadV1(HubProtocolModel):
    kind: Literal["cancel_run"]
    run_id: UUID


HubOpenClawOperationPayloadV1 = Annotated[
    Union[
        HubOpenClawModelPayloadV1,
        HubOpenClawLogsPayloadV1,
        HubOpenClawDiagnosePayloadV1,
        HubRunOncePayloadV1,
        HubCancelRunPayloadV1,
    ],
    Field(discriminator="kind"),
]


class HubOpenClawDeletedResultV1(HubProtocolModel):
    kind: Literal["instance_deleted"]
    instance_id: InstanceId
    archived: dict[str, str | None] = Field(default_factory=dict, max_length=8)


class HubOpenClawModelResultV1(HubProtocolModel):
    kind: Literal["model_updated"]
    model: str = Field(min_length=1, max_length=128)


class HubOpenClawPairResultV1(HubProtocolModel):
    kind: Literal["whatsapp_pair_started"]
    id: InstanceId
    message: str = Field(min_length=1, max_length=256)
    pair_pid: int = Field(ge=1)
    pair_log: Literal["logs/pair.log"]


class HubOpenClawLogsResultV1(HubProtocolModel):
    kind: Literal["instance_logs"]
    id: InstanceId
    gateway_log: str = Field(max_length=32768)
    pair_log: str = Field(max_length=32768)
    pair_qr_ascii: str = Field(max_length=8192)
    pair_status: str = Field(max_length=32)
    pair_hint: str | None = Field(default=None, max_length=512)
    gateway_log_path: Literal["logs/gateway.log"]
    pair_log_path: Literal["logs/pair.log"]
    events_log_path: Literal["logs/events.jsonl"]
    events_log: str = Field(max_length=32768)

    @model_validator(mode="after")
    def validate_total_size(self) -> HubOpenClawLogsResultV1:
        if sum(
            len(value.encode("utf-8"))
            for value in (self.gateway_log, self.pair_log, self.pair_qr_ascii, self.events_log)
        ) > 64 * 1024:
            raise ValueError("combined log result exceeds 64 KiB")
        return self


class HubOpenClawDiagnoseResultV1(HubProtocolModel):
    kind: Literal["instance_diagnosis"]
    id: InstanceId
    profile: str = Field(max_length=128)
    workload_type: str = Field(max_length=64)
    status: str = Field(max_length=32)
    port: int = Field(ge=1, le=65535)
    pid: int | None = Field(default=None, ge=1)
    gateway_target: str = Field(max_length=256)
    gateway_reachable: bool
    pair_status: str = Field(max_length=32)
    pair_hint: str | None = Field(default=None, max_length=512)
    whatsapp_running: bool | None = None
    whatsapp_connected: bool | None = None
    whatsapp_last_error: str | None = Field(default=None, max_length=1024)
    last_inbound_at: int | None = None
    last_outbound_at: int | None = None
    transport_established: bool
    router_api_key_present: bool
    no_api_key_error_seen: bool
    recommended_action: str | None = Field(default=None, max_length=256)
    auto_recover_triggered: bool
    auto_recover_message: str | None = Field(default=None, max_length=512)
    evidence: list[str] = Field(max_length=16)


class HubRunOnceResultV1(HubProtocolModel):
    """Terminal outcome of one run-once execution. `summary` must already be redacted."""

    kind: Literal["instance_run"]
    run_id: UUID
    state: Literal["succeeded", "failed", "cancelled", "unknown"]
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    log_ref: RunLogRef | None = None
    summary: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def validate_timeline(self) -> HubRunOnceResultV1:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if self.state == "succeeded" and self.exit_code not in (None, 0):
            raise ValueError("a succeeded run cannot report a non-zero exit_code")
        return self


class HubRunCancelledResultV1(HubProtocolModel):
    kind: Literal["run_cancelled"]
    run_id: UUID
    state: Literal["cancelled", "unknown", "already_finished"]


HubCommandResultV1 = Annotated[
    Union[
        HubOpenClawDeletedResultV1,
        HubOpenClawModelResultV1,
        HubOpenClawPairResultV1,
        HubOpenClawLogsResultV1,
        HubOpenClawDiagnoseResultV1,
        HubRunOnceResultV1,
        HubRunCancelledResultV1,
    ],
    Field(discriminator="kind"),
]
HubCommandResultAdapter = TypeAdapter(HubCommandResultV1)


def expected_result_kind(operation: HubCommandOperation) -> str | None:
    return {
        HubCommandOperation.DELETE_INSTANCE: "instance_deleted",
        HubCommandOperation.UPDATE_INSTANCE_MODEL: "model_updated",
        HubCommandOperation.GET_INSTANCE_LOGS: "instance_logs",
        HubCommandOperation.DIAGNOSE_INSTANCE: "instance_diagnosis",
        HubCommandOperation.PAIR_WHATSAPP: "whatsapp_pair_started",
        HubCommandOperation.RUN_INSTANCE_ONCE: "instance_run",
        HubCommandOperation.CANCEL_RUN: "run_cancelled",
    }.get(operation)


_OPERATIONS_REQUIRING_PAYLOAD = {
    HubCommandOperation.UPDATE_INSTANCE_MODEL,
    HubCommandOperation.GET_INSTANCE_LOGS,
    HubCommandOperation.DIAGNOSE_INSTANCE,
    HubCommandOperation.RUN_INSTANCE_ONCE,
    HubCommandOperation.CANCEL_RUN,
}
_EXPECTED_PAYLOAD_KIND = {
    HubCommandOperation.UPDATE_INSTANCE_MODEL: "update_model",
    HubCommandOperation.GET_INSTANCE_LOGS: "get_logs",
    HubCommandOperation.DIAGNOSE_INSTANCE: "diagnose",
    HubCommandOperation.RUN_INSTANCE_ONCE: "run_once",
    HubCommandOperation.CANCEL_RUN: "cancel_run",
}
# run_once / cancel_run payloads are runtime-agnostic; these remain OpenClaw-specific.
_OPENCLAW_ONLY_PAYLOAD_KINDS = {"update_model", "get_logs", "diagnose"}


def validate_command_payloads(
    operation: HubCommandOperation,
    runtime_type: str,
    create_payload: object | None,
    operation_payload: object | None,
) -> None:
    """Shared payload rules for issued and polled commands."""
    if (operation == HubCommandOperation.CREATE_INSTANCE) != (create_payload is not None):
        raise ValueError("create_payload must be present only for create_instance commands")
    if (operation in _OPERATIONS_REQUIRING_PAYLOAD) != (operation_payload is not None):
        raise ValueError("operation_payload must be present only for operations that require parameters")
    if create_payload is not None:
        create_kind = getattr(create_payload, "kind", None)
        if runtime_type == "openclaw":
            if create_kind == "cli_task":
                raise ValueError("OpenClaw instances are created with a whatsapp or dingtalk payload")
        elif runtime_type in CLI_RUNTIME_TYPES:
            if create_kind != "cli_task":
                raise ValueError("CLI task runtimes are created with a cli_task payload")
            if runtime_type == "codex" and getattr(create_payload, "claude_permission_mode", None) is not None:
                raise ValueError("claude_permission_mode does not apply to the codex runtime")
            if runtime_type == "claude-code" and getattr(create_payload, "codex_sandbox", None) is not None:
                raise ValueError("codex_sandbox does not apply to the claude-code runtime")
        else:
            raise ValueError("create_instance is not supported for this runtime type")
    payload_kind = getattr(operation_payload, "kind", None) if operation_payload is not None else None
    if payload_kind in _OPENCLAW_ONLY_PAYLOAD_KINDS and runtime_type != "openclaw":
        raise ValueError("operation_payload is currently supported only for OpenClaw")
    expected_kind = _EXPECTED_PAYLOAD_KIND.get(operation)
    if expected_kind and operation_payload is not None and payload_kind != expected_kind:
        raise ValueError("operation_payload kind does not match the command operation")


class HubCommandV1(HubProtocolModel):
    command_id: UUID
    target_instance_id: InstanceId
    runtime_type: RuntimeType
    operation: HubCommandOperation
    desired_revision: int = Field(ge=1)
    attempt: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    create_payload: HubCreatePayloadV1 | None = None
    operation_payload: HubOpenClawOperationPayloadV1 | None = None

    @model_validator(mode="after")
    def validate_expiration(self) -> HubCommandV1:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        validate_command_payloads(self.operation, self.runtime_type, self.create_payload, self.operation_payload)
        return self


class HubCommandIssueRequestV1(HubProtocolModel):
    target_instance_id: InstanceId
    runtime_type: RuntimeType
    operation: HubCommandOperation
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    desired_revision: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expires_at: AwareDatetime
    create_payload: HubCreatePayloadV1 | None = None
    operation_payload: HubOpenClawOperationPayloadV1 | None = None

    @model_validator(mode="after")
    def validate_operation_payload(self) -> HubCommandIssueRequestV1:
        validate_command_payloads(self.operation, self.runtime_type, self.create_payload, self.operation_payload)
        return self


class HubCredentialIssueRequestV1(HubProtocolModel):
    expires_in_seconds: int = Field(default=90 * 24 * 60 * 60, ge=300, le=365 * 24 * 60 * 60)


class HubCredentialIssuedV1(HubProtocolModel):
    credential_id: UUID
    hub_id: HubId
    token: str = Field(min_length=32, max_length=256)
    expires_at: AwareDatetime


class HubCredentialViewV1(HubProtocolModel):
    credential_id: UUID
    status: Literal["active", "expired", "revoked"]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_used_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None


class HubCommandReceiptV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    command_id: UUID
    status: Literal["pending", "running", "succeeded", "failed", "rejected", "unknown", "expired", "superseded"]
    issued_at: AwareDatetime
    expires_at: AwareDatetime


class HubCommandPollResponseV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    server_time: AwareDatetime
    commands: list[HubCommandV1] = Field(max_length=50)


class HubCommandLeaseRenewalRequestV1(HubProtocolModel):
    attempt: int = Field(ge=1)


class HubCommandLeaseV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    command_id: UUID
    attempt: int = Field(ge=1)
    server_time: AwareDatetime
    lease_expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_expiration(self) -> HubCommandLeaseV1:
        if self.lease_expires_at <= self.server_time:
            raise ValueError("lease_expires_at must be later than server_time")
        return self


class HubCommandEventV1(HubProtocolModel):
    type: Literal["command"]
    event_id: UUID
    sequence: int = Field(ge=1)
    occurred_at: AwareDatetime
    command_id: UUID
    desired_revision: int = Field(ge=1)
    attempt: int = Field(ge=1)
    phase: HubCommandPhase
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    observed_state: RuntimeObservedState | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Z0-9][A-Z0-9_-]*$")
    result: HubCommandResultV1 | None = None


class HubRuntimeStateEventV1(HubProtocolModel):
    type: Literal["runtime_state"]
    event_id: UUID
    sequence: int = Field(ge=1)
    occurred_at: AwareDatetime
    instance_id: InstanceId
    runtime_type: RuntimeType
    observed_state: RuntimeObservedState
    desired_revision: int | None = Field(default=None, ge=1)


class HubScheduledRunEventV1(HubProtocolModel):
    """Lifecycle of one scheduled or manual run, reported by the Hub that triggered it.

    Hub-local schedule triggers never originate from a Backend command, so this
    event is how the Backend learns about them at all. `(instance_id,
    scheduled_for)` is the idempotency key the Backend upserts on.
    """

    type: Literal["scheduled_run"]
    event_id: UUID
    sequence: int = Field(ge=1)
    occurred_at: AwareDatetime
    run_id: UUID
    instance_id: InstanceId
    runtime_type: RuntimeType
    trigger: RunTrigger
    scheduled_for: AwareDatetime
    state: RunState
    command_id: UUID | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    log_ref: RunLogRef | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Z0-9][A-Z0-9_-]*$")

    @model_validator(mode="after")
    def validate_state_fields(self) -> HubScheduledRunEventV1:
        terminal = self.state in {"succeeded", "failed", "cancelled", "unknown"}
        if terminal and self.started_at is not None and self.finished_at is None:
            raise ValueError("a run that started must report finished_at once it reaches a terminal state")
        if self.state == "queued" and (self.started_at or self.finished_at or self.exit_code is not None):
            raise ValueError("a queued run cannot report execution fields")
        if self.trigger == "manual" and self.command_id is None:
            raise ValueError("manual runs must reference the Backend command that requested them")
        return self


HubEventV1 = Annotated[
    Union[HubCommandEventV1, HubRuntimeStateEventV1, HubScheduledRunEventV1],
    Field(discriminator="type"),
]


class HubEventBatchRequestV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    after_sequence: int = Field(ge=0)
    events: list[HubEventV1] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_contiguous_sequence(self) -> HubEventBatchRequestV1:
        expected = self.after_sequence + 1
        event_ids: set[UUID] = set()
        for event in self.events:
            if event.sequence != expected:
                raise ValueError("event sequences must be contiguous from after_sequence")
            if event.event_id in event_ids:
                raise ValueError("event ids must be unique within a batch")
            expected += 1
            event_ids.add(event.event_id)
        return self


class HubEventBatchAckV1(HubProtocolModel):
    protocol_version: Literal["1.0"]
    accepted_through_sequence: int = Field(ge=0)
    accepted_at: AwareDatetime


HubMessageV1 = Union[
    HubHeartbeatRequestV1,
    HubHeartbeatResponseV1,
    HubInventorySnapshotV1,
    HubInventoryAckV1,
    HubCommandV1,
    HubCommandIssueRequestV1,
    HubCommandReceiptV1,
    HubCredentialIssueRequestV1,
    HubCredentialIssuedV1,
    HubCredentialViewV1,
    HubCommandPollResponseV1,
    HubCommandLeaseRenewalRequestV1,
    HubCommandLeaseV1,
    HubEventBatchRequestV1,
    HubEventBatchAckV1,
]


def hub_protocol_json_schema() -> dict:
    schema = TypeAdapter(HubMessageV1).json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Orchestrator Hub Protocol v1.0",
        **schema,
    }
