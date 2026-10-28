export type AuthSession = {
  wallet_id: string
  chain_id: string | null
  auth_type?: string
  issued_at: string
  expires_at: string
  ucan_session?: unknown
  ucan_signature?: unknown
}

export type SessionCapability = {
  protocol: 'cookie-wallet' | 'ucan'
  hasUcanSession: boolean
  hasUcanSignature: boolean
}

export type PlatformSession = {
  isAuthenticated: boolean
  walletId: string | null
  walletShort: string | null
  chainId: string | null
  authType: string | null
  issuedAt: string | null
  expiresAt: string | null
  capability: SessionCapability
  raw: AuthSession | null
}

export type AuthChallenge = {
  wallet_id: string
  chain_id: string | null
  auth_type: string
  challenge: string
  challenge_token: string
  issued_at: string
  expires_at: string
}

export type AgentListItem = {
  key: string
  display_name: string
  category: string
  path: string
  available: boolean
}

export type AgentListResponse = {
  items: AgentListItem[]
}

export type AgentWorkspaceSummary = {
  available: boolean
  broker: string
  running: boolean
  pid: number | null
  runtime_dir: string
  strategy_file: string
  state_file: string
  service_log_path: string
  strategies: Array<Record<string, unknown>>
  state: Record<string, unknown>
  recent_signals: Array<Record<string, unknown>>
  recent_orders: Array<Record<string, unknown>>
  recent_runs: Array<Record<string, unknown>>
  order_ledger_entries: Array<Record<string, unknown>>
  diagnostics: Record<string, unknown>
  market_observation: Record<string, unknown>
  watchlist_candidates: Array<Record<string, unknown>>
  candidate_review: Record<string, unknown>
  candidate_reviews: Array<Record<string, unknown>>
  service_log_tail: string
  strategy_count: number
  signal_count: number
  order_count: number
  last_signal_at: string | null
  last_order_at: string | null
  last_run_at: string | null
  last_action: string | null
  last_reason: string | null
  active_position_quantity: number
  strategy_snapshots: Array<Record<string, unknown>>
  last_cycle_strategy_count: number
  last_cycle_request_count: number
  last_snapshot_path: string | null
}

export type AgentWorkspaceActionResponse = {
  executed: boolean
  action: string
  stdout: string
}

export type AgentWorkspaceConfigUpdateResponse = {
  saved: boolean
  broker: string
  strategyCount: number
}

export type BotInstanceView = {
  id: string
  kind: string
  name: string
  profile: string
  model: string
  status: string
  owner_wallet: string
  created_at: string
  updated_at: string
  port: number
  pid: number | null
  root_dir: string
  logs_dir: string
  last_error: string | null
  dingtalk_client_id?: string | null
}

export type BotInstanceListResponse = {
  defaultModel: string
  items: BotInstanceView[]
}

export type RuntimeProvider = {
  id: string
  name: string
  category: string
  management_mode: string
  availability: string
  capabilities: string[]
}

export type RuntimeProviderListResponse = {
  items: RuntimeProvider[]
}

export type RuntimeInstance = {
  id: string
  name: string
  runtime_type: string
  workload_type: string
  model: string
  desired_state: string
  observed_state: string
  owner_wallet: string
  endpoint: string
  process_id: number | null
  last_error: string | null
  created_at: string
  updated_at: string
  capabilities: string[]
}

export type RuntimeInstanceListResponse = {
  items: RuntimeInstance[]
  total: number
  running: number
  stopped: number
  attention: number
}

export type BotInstanceLogsResponse = {
  id: string
  gateway_log: string
  pair_log: string
  pair_qr_ascii: string
  pair_status: string
  pair_hint: string | null
  gateway_log_path: string
  pair_log_path: string
  events_log_path: string
  events_log: string
}

export type BotInstanceActionResponse = {
  message: string
  instance: BotInstanceView
}

export type BotInstanceCreateRequest = {
  kind: string
  name: string
  model?: string | null
  template?: string | null
  dingtalk_client_id?: string | null
  dingtalk_client_secret?: string | null
}

export type BotInstancePairResponse = {
  message: string
  pair_pid: number
  pair_log: string
}

export type BotInstanceDiagnoseResponse = {
  id: string
  profile: string
  kind: string
  status: string
  port: number
  pid: number | null
  gateway_target: string
  gateway_reachable: boolean
  pair_status: string
  pair_hint: string | null
  whatsapp_running: boolean | null
  whatsapp_connected: boolean | null
  whatsapp_last_error: string | null
  last_inbound_at: number | null
  last_outbound_at: number | null
  transport_established: boolean
  router_api_key_present: boolean
  no_api_key_error_seen: boolean
  recommended_action: string | null
  auto_recover_triggered: boolean
  auto_recover_message: string | null
  evidence: string[]
}

export type AgentTypeItem = {
  id: string
  name: string
  requires: string[]
}

export type AgentTypesResponse = {
  agentTypes: AgentTypeItem[]
}
