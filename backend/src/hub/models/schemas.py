from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: bool = True
    service: str = "agent-hub"


class VersionResponse(BaseModel):
    name: str = "agent-hub"
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


class BotInstanceView(BaseModel):
    id: str
    kind: str
    name: str
    profile: str
    model: str
    status: str
    owner_wallet: str
    created_at: str
    updated_at: str
    port: int
    pid: int | None
    root_dir: str
    logs_dir: str
    last_error: str | None
    dingtalk_client_id: str | None = None


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
