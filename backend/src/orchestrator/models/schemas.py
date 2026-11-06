from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator


class HealthResponse(BaseModel):
    ok: bool = True
    service: str = "orchestrator"


class VersionResponse(BaseModel):
    name: str = "orchestrator"
    version: str = "0.1.0"
    runtime: str = "python"


class AuthChallengeRequest(BaseModel):
    wallet_id: str
    chain_id: str | None = None


class AuthChallengeView(BaseModel):
    wallet_id: str
    chain_id: str | None = None
    auth_type: str = "wallet_plugin"
    challenge: str
    challenge_token: str
    issued_at: str
    expires_at: str


class IdentityLoginSessionView(BaseModel):
    session_id: str
    request_id: str | None = None
    nonce: str | None = None
    audience: str | None = None
    scopes: list[str] = Field(default_factory=list)
    verify_url: str | None = None
    expires_at: str
    poll_interval: int = 2


class IdentityLoginSessionEnvelope(BaseModel):
    code: int = 0
    message: str = "ok"
    data: IdentityLoginSessionView


class IdentityLoginResult(BaseModel):
    token: str
    did: str
    walletAddress: str | None = None
    wallet_address: str | None = None
    username: str | None = None
    expires_at: str
    refresh_expires_at: str | None = None
    auth_type: str = "wallet_identity"
    auth_method: str | None = None


class IdentityLoginResultEnvelope(BaseModel):
    code: int = 0
    message: str = "ok"
    data: IdentityLoginResult


class AuthSessionVerifyRequest(BaseModel):
    wallet_id: str
    chain_id: str | None = None
    challenge: str
    challenge_token: str
    signature: str
    ucan_session: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    ucan_signature: dict[str, Any] | list[Any] | str | int | float | bool | None = None


class AuthSessionView(BaseModel):
    wallet_id: str
    chain_id: str | None = None
    auth_type: str = "wallet_plugin"
    auth_method: str | None = None
    did: str | None = None
    username: str | None = None
    issued_at: str
    expires_at: str
    ucan_session: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    ucan_signature: dict[str, Any] | list[Any] | str | int | float | bool | None = None


class AgentWorkspaceSummaryResponse(BaseModel):
    available: bool
    broker: str
    running: bool
    pid: int | None
    runtime_dir: str
    strategy_file: str
    state_file: str
    service_log_path: str
    strategies: list[dict[str, Any] | list[Any] | str | int | float | bool | None]
    state: dict[str, Any]
    recent_signals: list[dict[str, Any]]
    recent_orders: list[dict[str, Any]]
    recent_runs: list[dict[str, Any]] = []
    order_ledger_entries: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    market_observation: dict[str, Any] = {}
    watchlist_candidates: list[dict[str, Any]] = []
    candidate_review: dict[str, Any] = {}
    candidate_reviews: list[dict[str, Any]] = []
    service_log_tail: str
    strategy_count: int = 0
    signal_count: int = 0
    order_count: int = 0
    last_signal_at: str | None = None
    last_order_at: str | None = None
    last_run_at: str | None = None
    last_action: str | None = None
    last_reason: str | None = None
    active_position_quantity: int = 0
    strategy_snapshots: list[dict[str, Any]] = []
    last_cycle_strategy_count: int = 0
    last_cycle_request_count: int = 0
    last_snapshot_path: str | None = None


class AgentWorkspaceConfigUpdateRequest(BaseModel):
    broker: str
    strategy_id: str | None = None
    strategy: dict[str, Any]


class AgentWorkspaceConfigUpdateResponse(BaseModel):
    saved: bool
    broker: str
    strategyCount: int


class AgentWorkspaceOrderUnlockRequest(BaseModel):
    trading_day: str
    strategy_id: str
    symbol: str
    side: str
    reason: str
    actor: str = "hub"


class AgentWorkspaceActionResponse(BaseModel):
    executed: bool
    action: str
    stdout: str


class AgentListItem(BaseModel):
    key: str
    display_name: str
    category: str
    path: str
    available: bool


class AgentListResponse(BaseModel):
    items: list[AgentListItem]


class RouterModelsResponse(BaseModel):
    models: list[str]


class AgentTypeItem(BaseModel):
    id: str
    name: str
    requires: list[str]


class AgentTypesResponse(BaseModel):
    agentTypes: list[AgentTypeItem]


class InstanceSchedule(BaseModel):
    """Schedule for a `scheduled` instance. Validated at the API edge; the Hub triggers it locally."""

    kind: Literal["cron", "interval"]
    expr: str | None = Field(default=None, min_length=1, max_length=128)
    seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_definition(self) -> "InstanceSchedule":
        if self.kind == "cron":
            if self.expr is None or self.seconds is not None:
                raise ValueError("cron schedule requires expr and must not set seconds")
            if not croniter.is_valid(self.expr):
                raise ValueError(f"invalid cron expression: {self.expr!r}")
        else:
            if self.seconds is None or self.expr is not None:
                raise ValueError("interval schedule requires seconds and must not set expr")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown timezone: {self.timezone!r}") from exc
        return self


class BotInstanceView(BaseModel):
    id: str
    kind: str
    name: str
    profile: str
    model: str
    status: str
    desired_state: str = "unknown"
    owner_wallet: str
    created_at: str
    updated_at: str
    port: int
    pid: int | None
    root_dir: str
    logs_dir: str
    last_error: str | None
    dingtalk_client_id: str | None = None
    deployment_mode: Literal["service", "scheduled"] = "service"
    schedule: InstanceSchedule | None = None


class BotInstanceListResponse(BaseModel):
    defaultModel: str
    items: list[BotInstanceView]


class BotInstanceLogsResponse(BaseModel):
    id: str
    gateway_log: str
    pair_log: str
    pair_qr_ascii: str
    pair_status: str
    pair_hint: str | None
    gateway_log_path: str
    pair_log_path: str
    events_log_path: str
    events_log: str


class BotInstanceCreateRequest(BaseModel):
    kind: str
    name: str
    model: str | None = None
    template: str | None = None
    dingtalk_client_id: str | None = None
    dingtalk_client_secret: str | None = None
    deployment_mode: Literal["service", "scheduled"] = "service"
    schedule: InstanceSchedule | None = None

    @model_validator(mode="after")
    def validate_schedule_matches_mode(self) -> "BotInstanceCreateRequest":
        if self.deployment_mode == "scheduled" and self.schedule is None:
            raise ValueError("scheduled instances require a schedule")
        if self.deployment_mode == "service" and self.schedule is not None:
            raise ValueError("service instances must not define a schedule")
        return self


class RuntimeProviderView(BaseModel):
    id: str
    name: str
    category: str
    management_mode: str
    availability: str
    capabilities: list[str]
    # Hubs that reported this runtime adapter and can therefore host it.
    hub_ids: list[str] = []


class RuntimeProviderListResponse(BaseModel):
    items: list[RuntimeProviderView]


class CliTaskSpec(BaseModel):
    """Definition of a one-shot CLI agent task; the prompt is fixed per instance, never per run."""

    prompt: str = Field(min_length=1, max_length=8192)
    codex_sandbox: Literal["read-only", "workspace-write"] | None = None
    claude_permission_mode: Literal["plan", "acceptEdits", "dontAsk"] | None = None
    run_timeout_seconds: int = Field(default=900, ge=30, le=86_400)
    # Plaintext model-provider credential; encrypted to the target Hub before it leaves the Backend.
    model_api_key: str | None = Field(default=None, min_length=1, max_length=1024)


class CliTaskView(BaseModel):
    prompt: str
    policy: str | None
    run_timeout_seconds: int
    model_credential_configured: bool = False


class RuntimeInstanceView(BaseModel):
    id: str
    name: str
    runtime_type: str
    workload_type: str
    model: str
    desired_state: str
    observed_state: str
    owner_wallet: str
    endpoint: str
    process_id: int | None
    last_error: str | None
    created_at: str
    updated_at: str
    capabilities: list[str]
    deployment_mode: Literal["service", "scheduled"] = "service"
    schedule: InstanceSchedule | None = None
    # True once the catalog holds at least one run for this instance.
    schedule_executed: bool = False
    latest_run_state: str | None = None
    latest_run_at: str | None = None
    task: CliTaskView | None = None
    hub_id: str | None = None
    hub_status: str = "unknown"
    hub_last_seen_at: str | None = None
    latest_command_status: str | None = None
    latest_command_operation: str | None = None
    latest_command_updated_at: str | None = None


class RuntimeHubView(BaseModel):
    id: str
    status: str
    last_seen_at: str | None
    adapter_count: int
    instance_count: int
    runtime_types: list[str] = []


class RuntimeInstanceListResponse(BaseModel):
    items: list[RuntimeInstanceView]
    total: int
    running: int
    stopped: int
    attention: int


class RuntimeHubListResponse(BaseModel):
    items: list[RuntimeHubView]
    total: int
    online: int
    offline: int
    unknown: int


class RuntimeInstanceActionResponse(BaseModel):
    message: str
    instance: RuntimeInstanceView
    run_id: str | None = None


class RuntimeInstanceCreateRequest(BotInstanceCreateRequest):
    runtime_type: str = "openclaw"
    task: CliTaskSpec | None = None
    # Target machine for placement; when several Hubs can run this runtime it must be set.
    target_hub_id: str | None = None

    @model_validator(mode="after")
    def validate_runtime_shape(self) -> "RuntimeInstanceCreateRequest":
        if self.runtime_type in {"codex", "claude-code"}:
            if self.task is None:
                raise ValueError("CLI task runtimes require a task definition")
            if self.kind != "cli_task":
                raise ValueError("CLI task instances use kind=cli_task")
            if self.deployment_mode != "scheduled":
                raise ValueError("CLI task instances are always scheduled")
            if self.runtime_type == "codex" and self.task.claude_permission_mode is not None:
                raise ValueError("claude_permission_mode does not apply to the codex runtime")
            if self.runtime_type == "claude-code" and self.task.codex_sandbox is not None:
                raise ValueError("codex_sandbox does not apply to the claude-code runtime")
            if self.dingtalk_client_id or self.dingtalk_client_secret or self.template:
                raise ValueError("messenger fields do not apply to CLI task runtimes")
        elif self.task is not None:
            raise ValueError("task definitions apply only to CLI task runtimes")
        return self


class ScheduledRunView(BaseModel):
    id: str
    instance_id: str
    trigger: Literal["schedule", "manual"]
    scheduled_for: str
    started_at: str | None
    finished_at: str | None
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
    exit_code: int | None
    log_ref: str | None
    command_id: str | None


class ScheduledRunListResponse(BaseModel):
    items: list[ScheduledRunView]
    total: int


class RuntimeNodeProviderReport(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str | None = Field(default=None, max_length=128)
    capabilities: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=64)


class RuntimeNodeHeartbeatRequest(BaseModel):
    agent_version: str = Field(min_length=1, max_length=128)
    providers: list[RuntimeNodeProviderReport] = Field(default_factory=list, max_length=32)
    resources: dict[str, int | float | str | bool | None] = Field(default_factory=dict, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("resources")
    @classmethod
    def validate_resources(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(not key or len(key) > 64 for key in value):
            raise ValueError("resource keys must contain 1 to 64 characters")
        if any(isinstance(item, str) and len(item) > 128 for item in value.values()):
            raise ValueError("resource strings must not exceed 128 characters")
        return value

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 64 or len(item) > 128 for key, item in value.items()):
            raise ValueError("label keys must be 1 to 64 characters and values at most 128 characters")
        return value

    @field_validator("providers")
    @classmethod
    def validate_unique_providers(cls, value: list[RuntimeNodeProviderReport]) -> list[RuntimeNodeProviderReport]:
        ids = [provider.id for provider in value]
        if len(ids) != len(set(ids)):
            raise ValueError("provider ids must be unique")
        return value


class RuntimeNodeView(BaseModel):
    id: str
    status: str
    last_seen_at: str | None
    agent_version: str | None
    providers: list[RuntimeNodeProviderReport]
    resources: dict[str, int | float | str | bool | None]
    labels: dict[str, str]


class RuntimeNodeListResponse(BaseModel):
    items: list[RuntimeNodeView]
    total: int
    online: int
    offline: int
    unknown: int


class BotInstanceUpdateModelRequest(BaseModel):
    model: str


class BotInstanceActionResponse(BaseModel):
    message: str
    instance: BotInstanceView


class BotInstancePairResponse(BaseModel):
    message: str
    pair_pid: int
    pair_log: str


class BotInstanceDiagnoseResponse(BaseModel):
    id: str
    profile: str
    kind: str
    status: str
    port: int
    pid: int | None
    gateway_target: str
    gateway_reachable: bool
    pair_status: str
    pair_hint: str | None
    whatsapp_running: bool | None
    whatsapp_connected: bool | None
    whatsapp_last_error: str | None
    last_inbound_at: int | None
    last_outbound_at: int | None
    transport_established: bool
    router_api_key_present: bool
    no_api_key_error_seen: bool
    recommended_action: str | None
    auto_recover_triggered: bool
    auto_recover_message: str | None
    evidence: list[str]
