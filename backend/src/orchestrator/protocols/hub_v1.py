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

    @model_validator(mode="after")
    def validate_credentials(self) -> HubOpenClawCreatePayloadV1:
        if self.kind == "dingtalk":
            if not self.dingtalk_client_id or not self.dingtalk_client_secret_envelope:
                raise ValueError("DingTalk creation requires an encrypted client secret")
        elif self.dingtalk_client_id or self.dingtalk_client_secret_envelope:
            raise ValueError("DingTalk credentials are only valid for the DingTalk workload")
        return self


class HubOpenClawModelPayloadV1(HubProtocolModel):
    kind: Literal["update_model"]
    model: str = Field(min_length=1, max_length=128)


class HubOpenClawLogsPayloadV1(HubProtocolModel):
    kind: Literal["get_logs"]
    lines: int = Field(default=120, ge=1, le=1000)


class HubOpenClawDiagnosePayloadV1(HubProtocolModel):
    kind: Literal["diagnose"]
    auto_recover: bool = False


HubOpenClawOperationPayloadV1 = Annotated[
    Union[HubOpenClawModelPayloadV1, HubOpenClawLogsPayloadV1, HubOpenClawDiagnosePayloadV1],
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


HubCommandResultV1 = Annotated[
    Union[
        HubOpenClawDeletedResultV1,
        HubOpenClawModelResultV1,
        HubOpenClawPairResultV1,
        HubOpenClawLogsResultV1,
        HubOpenClawDiagnoseResultV1,
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
    }.get(operation)


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
    create_payload: HubOpenClawCreatePayloadV1 | None = None
    operation_payload: HubOpenClawOperationPayloadV1 | None = None

    @model_validator(mode="after")
    def validate_expiration(self) -> HubCommandV1:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if (self.operation == HubCommandOperation.CREATE_INSTANCE) != (self.create_payload is not None):
            raise ValueError("create_payload must be present only for create_instance commands")
        if (self.operation in {
            HubCommandOperation.UPDATE_INSTANCE_MODEL,
            HubCommandOperation.GET_INSTANCE_LOGS,
            HubCommandOperation.DIAGNOSE_INSTANCE,
        }) != (self.operation_payload is not None):
            raise ValueError("operation_payload must be present only for operations that require parameters")
        if self.operation == HubCommandOperation.CREATE_INSTANCE and self.runtime_type != "openclaw":
            raise ValueError("create_instance is currently supported only for OpenClaw")
        if self.operation_payload is not None and self.runtime_type != "openclaw":
            raise ValueError("operation_payload is currently supported only for OpenClaw")
        expected_kind = {
            HubCommandOperation.UPDATE_INSTANCE_MODEL: "update_model",
            HubCommandOperation.GET_INSTANCE_LOGS: "get_logs",
            HubCommandOperation.DIAGNOSE_INSTANCE: "diagnose",
        }.get(self.operation)
        if expected_kind and self.operation_payload and self.operation_payload.kind != expected_kind:
            raise ValueError("operation_payload kind does not match the command operation")
        return self


class HubCommandIssueRequestV1(HubProtocolModel):
    target_instance_id: InstanceId
    runtime_type: RuntimeType
    operation: HubCommandOperation
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    desired_revision: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expires_at: AwareDatetime
    create_payload: HubOpenClawCreatePayloadV1 | None = None
    operation_payload: HubOpenClawOperationPayloadV1 | None = None

    @model_validator(mode="after")
    def validate_operation_payload(self) -> HubCommandIssueRequestV1:
        if (self.operation == HubCommandOperation.CREATE_INSTANCE) != (self.create_payload is not None):
            raise ValueError("create_payload must be present only for create_instance commands")
        if (self.operation in {
            HubCommandOperation.UPDATE_INSTANCE_MODEL,
            HubCommandOperation.GET_INSTANCE_LOGS,
            HubCommandOperation.DIAGNOSE_INSTANCE,
        }) != (self.operation_payload is not None):
            raise ValueError("operation_payload must be present only for operations that require parameters")
        if self.operation == HubCommandOperation.CREATE_INSTANCE and self.runtime_type != "openclaw":
            raise ValueError("create_instance is currently supported only for OpenClaw")
        if self.operation_payload is not None and self.runtime_type != "openclaw":
            raise ValueError("operation_payload is currently supported only for OpenClaw")
        expected_kind = {
            HubCommandOperation.UPDATE_INSTANCE_MODEL: "update_model",
            HubCommandOperation.GET_INSTANCE_LOGS: "get_logs",
            HubCommandOperation.DIAGNOSE_INSTANCE: "diagnose",
        }.get(self.operation)
        if expected_kind and self.operation_payload and self.operation_payload.kind != expected_kind:
            raise ValueError("operation_payload kind does not match the command operation")
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


HubEventV1 = Annotated[Union[HubCommandEventV1, HubRuntimeStateEventV1], Field(discriminator="type")]


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
