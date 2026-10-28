export type RecordItem = {
  id: string
  kind: 'signal' | 'order'
  title: string
  summary: string
  timestamp: string
  payload: Record<string, unknown>
}

export type StrategySnapshot = {
  id: string
  name: string
  symbol: string
  strategy: string
  timeframe: string
  enabled: boolean
  lastAction: string
  lastReason: string
  latestPrice: number | null
  observedAt: string | null
  riskOk: boolean | null
  riskReason: string | null
  positionQuantity: number
}

export type StrategyOption = {
  id: string
  name: string
  symbol: string
  enabled: boolean
  lastAction: string
  observedAt: string | null
  positionQuantity: number
  payload: Record<string, unknown>
}

export type TraderRunBatch = {
  runId: string
  startedAt: string | null
  finishedAt: string | null
  strategyCount: number
  signalCount: number
  orderPlanCount: number
  orderCount: number
  suppressedOrderCount: number
  errorCount: number
  summaryPath: string
}

export type TraderMarketObservation = {
  candidateCount: number
  tradePermission: string
  gradeCounts: {
    A: number
    B: number
    C: number
  }
  errorCount: number
  payload: Record<string, unknown>
}

export type WatchlistCandidate = {
  strategyId: string
  symbol: string
  name: string
  grade: string
  score: number
  tradePermission: string
  latestPrice: number | null
  pctChange: number | null
  signalAction: string
  signalReason: string
  riskOk: boolean | null
  riskReason: string
  reasons: string[]
  riskFlags: string[]
  payload: Record<string, unknown>
}

export type CandidateReviewSummary = {
  reviewCount: number
  positiveCount: number
  failedCount: number
  averageReturnPct: number | null
  payload: Record<string, unknown>
}

export type OrderLedgerEntry = {
  tradingDay: string
  orderKey: string
  strategyId: string
  symbol: string
  side: string
  quantity: number
  latestPrice: number | null
  lifecycleStatus: string
  guardActive: boolean
  retryAllowed: boolean
  orderFingerprint: string
  brokerOrderStatus: string
  brokerOrderId: string
  brokerProvider: string
  accepted: string
  recordedAt: string | null
  submittedAt: string | null
  filledAt: string | null
  unlockedAt: string | null
  unlockReason: string
  payload: Record<string, unknown>
}

export type TraderDiagnostics = {
  ok: boolean
  errors: string[]
  warnings: string[]
  brokerProvider: string
  brokerSupported: boolean
  brokerConfigured: boolean | null
  dataProvider: string
  dataBaseUrl: string
  accessTokenConfigured: boolean
  refreshTokenConfigured: boolean
  strategyCount: number
  enabledStrategyCount: number
  disabledStrategyCount: number
  runtimeDir: string
  runtimeDirExists: boolean
  strategyFile: string
  envFileExists: boolean
  stateFileExists: boolean
  payload: Record<string, unknown>
}

export type TraderConfigForm = {
  broker: string
  id: string
  enabled: boolean
  market: string
  symbol: string
  name: string
  timeframe: string
  strategy: string
  history_window: number
  breakout_lookback: number
  quantity: number
  position_quantity: number
  max_position: number
  stop_loss_pct: number
  take_profit_pct: number
  dry_run: boolean
  enable_buy: boolean
  active_lookback_days: number
  active_threshold_pct: number
  breakout_buffer_pct: number
  resistance_buffer_pct: number
  auction_lock_time: string
  auction_open_end_time: string
  auction_zero_threshold_pct: number
  auction_strong_open_threshold_pct: number
  afternoon_exit_time: string
  limit_up_threshold_pct: number
}

export type RecordFilter = 'all' | 'signal' | 'order'

export type RecordDetailField = {
  label: string
  value: string
}

function parseTimestamp(value: string): Date | null {
  const trimmed = value.trim()
  if (!trimmed || trimmed === '-') {
    return null
  }

  const direct = new Date(trimmed)
  if (!Number.isNaN(direct.getTime())) {
    return direct
  }

  const normalized = trimmed.includes('T') ? trimmed : trimmed.replace(' ', 'T')
  const localDate = new Date(normalized)
  if (!Number.isNaN(localDate.getTime())) {
    return localDate
  }

  return null
}

export function formatBrowserDateTime(value: string | null | undefined) {
  if (!value) {
    return '-'
  }
  const parsed = parseTimestamp(value)
  if (!parsed) {
    return value
  }
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(parsed)
}

export function displayPath(path: string | undefined) {
  if (!path) {
    return '-'
  }
  const parts = path.split('/').filter(Boolean)
  if (parts.length <= 3) {
    return path
  }
  return `.../${parts.slice(-3).join('/')}`
}

export function pickText(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) {
      return value
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value)
    }
  }
  return '-'
}

export function asNumber(value: unknown, fallback: number) {
  return typeof value === 'number' ? value : fallback
}

export function asBoolean(value: unknown, fallback: boolean) {
  return typeof value === 'boolean' ? value : fallback
}

export function buildRecordItems(summary: { recent_signals?: Array<Record<string, unknown>>; recent_orders?: Array<Record<string, unknown>> } | undefined): RecordItem[] {
  const signals = (summary?.recent_signals ?? []).map((item, index) => ({
    id: `signal-${index}-${pickText(item, ['ts', 'timestamp', 'symbol'])}`,
    kind: 'signal' as const,
    title: `${pickText(item, ['symbol', 'code', 'name'])} 信号`,
    summary: `${pickText(item, ['action', 'signal', 'decision'])} · ${pickText(item, ['strategyId', 'strategy', 'strategy_id'])}`,
    timestamp: pickText(item, ['ts', 'timestamp', 'created_at', 'time']),
    payload: item,
  }))
  const orders = (summary?.recent_orders ?? []).map((item, index) => ({
    id: `order-${index}-${pickText(item, ['ts', 'timestamp', 'symbol'])}`,
    kind: 'order' as const,
    title: `${pickText(item, ['symbol', 'code', 'name'])} 订单`,
    summary: `${pickText(item, ['side', 'action'])} · ${pickText(item, ['strategyId', 'status', 'broker', 'result'])}`,
    timestamp: pickText(item, ['ts', 'timestamp', 'created_at', 'time']),
    payload: item,
  }))
  return [...orders, ...signals].sort((left, right) => right.timestamp.localeCompare(left.timestamp))
}

export function buildStrategySnapshots(summary: { strategy_snapshots?: Array<Record<string, unknown>> } | undefined): StrategySnapshot[] {
  return (summary?.strategy_snapshots ?? []).map((item, index) => ({
    id: pickText(item, ['id']) === '-' ? `strategy-${index}` : pickText(item, ['id']),
    name: pickText(item, ['name']) === '-' ? '未命名策略' : pickText(item, ['name']),
    symbol: pickText(item, ['symbol']),
    strategy: pickText(item, ['strategy']),
    timeframe: pickText(item, ['timeframe']),
    enabled: asBoolean(item.enabled, true),
    lastAction: pickText(item, ['lastAction']),
    lastReason: pickText(item, ['lastReason']),
    latestPrice: typeof item.latestPrice === 'number' ? item.latestPrice : null,
    observedAt: pickText(item, ['observedAt']) === '-' ? null : pickText(item, ['observedAt']),
    riskOk: typeof item.riskOk === 'boolean' ? item.riskOk : null,
    riskReason: pickText(item, ['riskReason']) === '-' ? null : pickText(item, ['riskReason']),
    positionQuantity: asNumber(item.positionQuantity, 0),
  }))
}

export function buildStrategyOptions(
  summary: { strategies?: Array<Record<string, unknown>> } | undefined,
  strategySnapshots: StrategySnapshot[],
): StrategyOption[] {
  return ((summary?.strategies ?? []) as Array<Record<string, unknown>>).map((item, index) => {
    const id = pickText(item, ['id']) === '-' ? `strategy-${index}` : pickText(item, ['id'])
    const snapshot = strategySnapshots.find((entry) => entry.id === id) ?? null
    return {
      id,
      name: pickText(item, ['name']) === '-' ? `策略 ${index + 1}` : pickText(item, ['name']),
      symbol: pickText(item, ['symbol']),
      enabled: asBoolean(item.enabled, true),
      lastAction: snapshot?.lastAction ?? '-',
      observedAt: snapshot?.observedAt ?? null,
      positionQuantity: snapshot?.positionQuantity ?? 0,
      payload: item,
    }
  })
}

export function buildRunBatches(summary: { recent_runs?: Array<Record<string, unknown>> } | undefined): TraderRunBatch[] {
  return (summary?.recent_runs ?? []).map((item, index) => ({
    runId: pickText(item, ['run_id']) === '-' ? `run-${index}` : pickText(item, ['run_id']),
    startedAt: pickText(item, ['run_started_at']) === '-' ? null : pickText(item, ['run_started_at']),
    finishedAt: pickText(item, ['run_finished_at']) === '-' ? null : pickText(item, ['run_finished_at']),
    strategyCount: asNumber(item.strategy_count, 0),
    signalCount: asNumber(item.signal_count, 0),
    orderPlanCount: asNumber(item.order_plan_count, 0),
    orderCount: asNumber(item.order_count, 0),
    suppressedOrderCount: asNumber(item.suppressed_order_count, 0),
    errorCount: asNumber(item.error_count, 0),
    summaryPath: pickText(item, ['summary_path']),
  }))
}

export function buildMarketObservation(summary: { market_observation?: Record<string, unknown> } | undefined): TraderMarketObservation | null {
  const observation = summary?.market_observation
  if (!observation || !Object.keys(observation).length) {
    return null
  }
  const gradeCounts = pickRecord(observation, 'grade_counts')
  return {
    candidateCount: asNumber(observation.candidate_count, 0),
    tradePermission: pickText(observation, ['trade_permission']),
    gradeCounts: {
      A: asNumber(gradeCounts.A, 0),
      B: asNumber(gradeCounts.B, 0),
      C: asNumber(gradeCounts.C, 0),
    },
    errorCount: asNumber(observation.error_count, 0),
    payload: observation,
  }
}

export function buildWatchlistCandidates(summary: { watchlist_candidates?: Array<Record<string, unknown>> } | undefined): WatchlistCandidate[] {
  return (summary?.watchlist_candidates ?? []).map((item) => ({
    strategyId: pickText(item, ['strategy_id']),
    symbol: pickText(item, ['symbol']),
    name: pickText(item, ['name']),
    grade: pickText(item, ['grade']),
    score: asNumber(item.score, 0),
    tradePermission: pickText(item, ['trade_permission']),
    latestPrice: typeof item.latest_price === 'number' ? item.latest_price : null,
    pctChange: typeof item.pct_change === 'number' ? item.pct_change : null,
    signalAction: pickText(item, ['signal_action']),
    signalReason: pickText(item, ['signal_reason']),
    riskOk: typeof item.risk_ok === 'boolean' ? item.risk_ok : null,
    riskReason: pickText(item, ['risk_reason']),
    reasons: Array.isArray(item.reasons) ? item.reasons.map((value) => String(value)) : [],
    riskFlags: Array.isArray(item.risk_flags) ? item.risk_flags.map((value) => String(value)) : [],
    payload: item,
  }))
}

export function buildCandidateReviewSummary(summary: { candidate_review?: Record<string, unknown> } | undefined): CandidateReviewSummary | null {
  const review = summary?.candidate_review
  if (!review || !Object.keys(review).length) {
    return null
  }
  return {
    reviewCount: asNumber(review.review_count, 0),
    positiveCount: asNumber(review.positive_count, 0),
    failedCount: asNumber(review.failed_count, 0),
    averageReturnPct: typeof review.average_return_pct === 'number' ? review.average_return_pct : null,
    payload: review,
  }
}

export function buildOrderLedgerEntries(summary: { order_ledger_entries?: Array<Record<string, unknown>> } | undefined): OrderLedgerEntry[] {
  return (summary?.order_ledger_entries ?? []).map((item, index) => ({
    tradingDay: pickText(item, ['trading_day']) === '-' ? '-' : pickText(item, ['trading_day']),
    orderKey: pickText(item, ['order_key']) === '-' ? `ledger-${index}` : pickText(item, ['order_key']),
    strategyId: pickText(item, ['strategy_id']),
    symbol: pickText(item, ['symbol']),
    side: pickText(item, ['side']),
    quantity: asNumber(item.quantity, 0),
    latestPrice: typeof item.latest_price === 'number' ? item.latest_price : null,
    lifecycleStatus: pickText(item, ['lifecycle_status']),
    guardActive: asBoolean(item.guard_active, true),
    retryAllowed: asBoolean(item.retry_allowed, false),
    orderFingerprint: pickText(item, ['order_fingerprint']),
    brokerOrderStatus: pickText(item, ['broker_order_status']),
    brokerOrderId: pickText(item, ['broker_order_id']),
    brokerProvider: pickText(item, ['broker_provider']),
    accepted: pickText(item, ['accepted']),
    recordedAt: pickText(item, ['recorded_at']) === '-' ? null : pickText(item, ['recorded_at']),
    submittedAt: pickText(item, ['submitted_at']) === '-' ? null : pickText(item, ['submitted_at']),
    filledAt: pickText(item, ['filled_at']) === '-' ? null : pickText(item, ['filled_at']),
    unlockedAt: pickText(item, ['unlocked_at']) === '-' ? null : pickText(item, ['unlocked_at']),
    unlockReason: pickText(item, ['unlock_reason']),
    payload: item,
  }))
}

export function buildOrderLedgerDetailFields(entry: OrderLedgerEntry): RecordDetailField[] {
  return [
    { label: '交易日', value: entry.tradingDay },
    { label: '订单 Key', value: entry.orderKey },
    { label: '策略 ID', value: entry.strategyId },
    { label: '标的', value: entry.symbol },
    { label: '方向', value: entry.side },
    { label: '数量', value: String(entry.quantity) },
    { label: '最新价', value: entry.latestPrice == null ? '-' : String(entry.latestPrice) },
    { label: '生命周期', value: entry.lifecycleStatus },
    { label: '防线状态', value: entry.guardActive ? '锁定' : '已解锁' },
    { label: '允许重试', value: entry.retryAllowed ? '是' : '否' },
    { label: '订单指纹', value: entry.orderFingerprint },
    { label: '券商', value: entry.brokerProvider },
    { label: '券商状态', value: entry.brokerOrderStatus },
    { label: '券商订单号', value: entry.brokerOrderId },
    { label: 'Accepted', value: entry.accepted },
    { label: '记录时间', value: formatBrowserDateTime(entry.recordedAt) },
    { label: '提交时间', value: formatBrowserDateTime(entry.submittedAt) },
    { label: '成交时间', value: formatBrowserDateTime(entry.filledAt) },
    { label: '解锁时间', value: formatBrowserDateTime(entry.unlockedAt) },
    { label: '解锁原因', value: entry.unlockReason },
  ]
}

function pickRecord(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function pickStringArray(record: Record<string, unknown>, key: string): string[] {
  const value = record[key]
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

export function buildTraderDiagnostics(summary: { diagnostics?: Record<string, unknown> } | undefined): TraderDiagnostics | null {
  const diagnostics = summary?.diagnostics
  if (!diagnostics || typeof diagnostics !== 'object') {
    return null
  }
  const config = pickRecord(diagnostics, 'config')
  const dataSource = pickRecord(diagnostics, 'data_source')
  const broker = pickRecord(diagnostics, 'broker')
  const brokerStatus = pickRecord(broker, 'status')
  const runtime = pickRecord(diagnostics, 'runtime')
  const runtimeDir = pickRecord(runtime, 'runtime_dir')
  const stateFile = pickRecord(runtime, 'state_file')
  const strategies = pickRecord(diagnostics, 'strategies')
  return {
    ok: asBoolean(diagnostics.ok, false),
    errors: pickStringArray(diagnostics, 'errors'),
    warnings: pickStringArray(diagnostics, 'warnings'),
    brokerProvider: pickText(broker, ['provider']),
    brokerSupported: asBoolean(broker.supported, false),
    brokerConfigured: typeof brokerStatus.configured === 'boolean' ? brokerStatus.configured : null,
    dataProvider: pickText(dataSource, ['provider']),
    dataBaseUrl: pickText(dataSource, ['base_url']),
    accessTokenConfigured: asBoolean(dataSource.access_token_configured, false),
    refreshTokenConfigured: asBoolean(dataSource.refresh_token_configured, false),
    strategyCount: asNumber(strategies.count, 0),
    enabledStrategyCount: asNumber(strategies.enabled_count, 0),
    disabledStrategyCount: asNumber(strategies.disabled_count, 0),
    runtimeDir: pickText(config, ['runtime_dir']),
    runtimeDirExists: asBoolean(runtimeDir.exists, false),
    strategyFile: pickText(config, ['strategy_file']),
    envFileExists: asBoolean(config.env_file_exists, false),
    stateFileExists: asBoolean(stateFile.exists, false),
    payload: diagnostics,
  }
}

export function buildConfigDefaults(
  summary: { broker?: string; strategies?: Array<Record<string, unknown>> } | undefined,
  strategyId?: string | null,
): TraderConfigForm {
  const strategies = (summary?.strategies ?? []) as Array<Record<string, unknown>>
  const matchedStrategy =
    strategies.find((item) => pickText(item, ['id']) === strategyId) ??
    strategies[0] ??
    ({} as Record<string, unknown>)
  const strategy = matchedStrategy
  return {
    broker: summary?.broker ?? 'paper',
    id: pickText(strategy, ['id']) === '-' ? 'etf-breakout-demo' : pickText(strategy, ['id']),
    enabled: asBoolean(strategy.enabled, true),
    market: pickText(strategy, ['market']) === '-' ? 'a-share' : pickText(strategy, ['market']),
    symbol: pickText(strategy, ['symbol']) === '-' ? '' : pickText(strategy, ['symbol']),
    name: pickText(strategy, ['name']) === '-' ? '' : pickText(strategy, ['name']),
    timeframe: pickText(strategy, ['timeframe']) === '-' ? '1d' : pickText(strategy, ['timeframe']),
    strategy: pickText(strategy, ['strategy']) === '-' ? 'breakout' : pickText(strategy, ['strategy']),
    history_window: asNumber(strategy.history_window, 20),
    breakout_lookback: asNumber(strategy.breakout_lookback, 5),
    quantity: asNumber(strategy.quantity, 100),
    position_quantity: asNumber(strategy.position_quantity, 0),
    max_position: asNumber(strategy.max_position, 500),
    stop_loss_pct: asNumber(strategy.stop_loss_pct, 0.03),
    take_profit_pct: asNumber(strategy.take_profit_pct, 0.08),
    dry_run: asBoolean(strategy.dry_run, true),
    enable_buy: asBoolean(strategy.enable_buy, false),
    active_lookback_days: asNumber(strategy.active_lookback_days, 10),
    active_threshold_pct: asNumber(strategy.active_threshold_pct, 4),
    breakout_buffer_pct: asNumber(strategy.breakout_buffer_pct, 0.2),
    resistance_buffer_pct: asNumber(strategy.resistance_buffer_pct, 1),
    auction_lock_time:
      pickText(strategy, ['auction_lock_time']) === '-' ? '09:29' : pickText(strategy, ['auction_lock_time']),
    auction_open_end_time:
      pickText(strategy, ['auction_open_end_time']) === '-' ? '09:35' : pickText(strategy, ['auction_open_end_time']),
    auction_zero_threshold_pct: asNumber(strategy.auction_zero_threshold_pct, 0),
    auction_strong_open_threshold_pct: asNumber(strategy.auction_strong_open_threshold_pct, 3),
    afternoon_exit_time:
      pickText(strategy, ['afternoon_exit_time']) === '-' ? '14:30' : pickText(strategy, ['afternoon_exit_time']),
    limit_up_threshold_pct: asNumber(strategy.limit_up_threshold_pct, 9.8),
  }
}

export function buildNewStrategyDefaults(broker: string): TraderConfigForm {
  return {
    broker,
    id: '',
    enabled: true,
    market: 'a-share',
    symbol: '',
    name: '',
    timeframe: '1d',
    strategy: 'breakout',
    history_window: 20,
    breakout_lookback: 5,
    quantity: 100,
    position_quantity: 0,
    max_position: 500,
    stop_loss_pct: 0.03,
    take_profit_pct: 0.08,
    dry_run: true,
    enable_buy: false,
    active_lookback_days: 10,
    active_threshold_pct: 4,
    breakout_buffer_pct: 0.2,
    resistance_buffer_pct: 1,
    auction_lock_time: '09:29',
    auction_open_end_time: '09:35',
    auction_zero_threshold_pct: 0,
    auction_strong_open_threshold_pct: 3,
    afternoon_exit_time: '14:30',
    limit_up_threshold_pct: 9.8,
  }
}

export function buildCopiedStrategyDefaults(source: TraderConfigForm): TraderConfigForm {
  const nextId = source.id ? `${source.id}-copy` : ''
  return {
    ...source,
    id: nextId,
    name: source.name ? `${source.name} 副本` : '',
    enabled: false,
  }
}

export function buildRecordDetailFields(record: RecordItem): RecordDetailField[] {
  const payload = record.payload
  if (record.kind === 'signal') {
    return [
      { label: '策略 ID', value: pickText(payload, ['strategyId', 'strategy_id', 'strategy']) },
      { label: '动作', value: pickText(payload, ['action', 'signal', 'decision']) },
      { label: '标的', value: pickText(payload, ['symbol', 'code', 'name']) },
      { label: '价格', value: pickText(payload, ['price', 'lastPrice', 'latestPrice']) },
      { label: '时间', value: pickText(payload, ['ts', 'timestamp', 'created_at', 'time']) },
      { label: '原因', value: pickText(payload, ['reason', 'message', 'note']) },
    ]
  }

  return [
    { label: '策略 ID', value: pickText(payload, ['strategyId', 'strategy_id', 'strategy']) },
    { label: '方向', value: pickText(payload, ['side', 'action']) },
    { label: '状态', value: pickText(payload, ['status', 'result']) },
    { label: '标的', value: pickText(payload, ['symbol', 'code', 'name']) },
    { label: '数量', value: pickText(payload, ['quantity', 'qty', 'filledQuantity']) },
    { label: '券商', value: pickText(payload, ['broker', 'account', 'accountId']) },
    { label: '订单号', value: pickText(payload, ['orderId', 'order_id', 'id']) },
    { label: '时间', value: pickText(payload, ['ts', 'timestamp', 'created_at', 'time']) },
    { label: '原因', value: pickText(payload, ['reason', 'message', 'note']) },
  ]
}

export function pickRecordStrategyId(record: RecordItem | null): string | null {
  if (!record) {
    return null
  }
  const strategyId = pickText(record.payload, ['strategyId', 'strategy_id', 'strategy'])
  return strategyId === '-' ? null : strategyId
}

export function buildRecordContextFields(record: RecordItem): RecordDetailField[] {
  const payload = record.payload
  const ignoredKeys = new Set([
    'strategyId', 'strategy_id', 'strategy', 'action', 'signal', 'decision', 'side', 'status', 'result', 'symbol',
    'code', 'name', 'price', 'lastPrice', 'latestPrice', 'quantity', 'qty', 'filledQuantity', 'broker', 'account',
    'accountId', 'orderId', 'order_id', 'id', 'ts', 'timestamp', 'created_at', 'time', 'reason', 'message', 'note',
  ])

  return Object.entries(payload)
    .filter(([key]) => !ignoredKeys.has(key))
    .slice(0, 8)
    .map(([key, value]) => ({
      label: key,
      value: typeof value === 'string' ? value : JSON.stringify(value),
    }))
}

export function normalizeStrategyPayload(strategy: Record<string, unknown>, enabled: boolean) {
  return {
    ...strategy,
    id: pickText(strategy, ['id']) === '-' ? '' : pickText(strategy, ['id']),
    enabled,
    market: pickText(strategy, ['market']) === '-' ? 'a-share' : pickText(strategy, ['market']),
    symbol: pickText(strategy, ['symbol']) === '-' ? '' : pickText(strategy, ['symbol']),
    name: pickText(strategy, ['name']) === '-' ? '' : pickText(strategy, ['name']),
    timeframe: pickText(strategy, ['timeframe']) === '-' ? '1d' : pickText(strategy, ['timeframe']),
    strategy: pickText(strategy, ['strategy']) === '-' ? 'breakout' : pickText(strategy, ['strategy']),
    history_window: asNumber(strategy.history_window, 20),
    breakout_lookback: asNumber(strategy.breakout_lookback, 5),
    quantity: asNumber(strategy.quantity, 100),
    position_quantity: asNumber(strategy.position_quantity, 0),
    max_position: asNumber(strategy.max_position, 500),
    stop_loss_pct: asNumber(strategy.stop_loss_pct, 0.03),
    take_profit_pct: asNumber(strategy.take_profit_pct, 0.08),
    dry_run: asBoolean(strategy.dry_run, true),
    enable_buy: asBoolean(strategy.enable_buy, false),
    active_lookback_days: asNumber(strategy.active_lookback_days, 10),
    active_threshold_pct: asNumber(strategy.active_threshold_pct, 4),
    breakout_buffer_pct: asNumber(strategy.breakout_buffer_pct, 0.2),
    resistance_buffer_pct: asNumber(strategy.resistance_buffer_pct, 1),
    auction_lock_time:
      pickText(strategy, ['auction_lock_time']) === '-' ? '09:29' : pickText(strategy, ['auction_lock_time']),
    auction_open_end_time:
      pickText(strategy, ['auction_open_end_time']) === '-' ? '09:35' : pickText(strategy, ['auction_open_end_time']),
    auction_zero_threshold_pct: asNumber(strategy.auction_zero_threshold_pct, 0),
    auction_strong_open_threshold_pct: asNumber(strategy.auction_strong_open_threshold_pct, 3),
    afternoon_exit_time:
      pickText(strategy, ['afternoon_exit_time']) === '-' ? '14:30' : pickText(strategy, ['afternoon_exit_time']),
    limit_up_threshold_pct: asNumber(strategy.limit_up_threshold_pct, 9.8),
  }
}

export function buildRecentStrategyRecords(records: RecordItem[], strategyId: string | null) {
  if (!strategyId) {
    return []
  }
  return records.filter((record) => pickText(record.payload, ['strategyId', 'strategy_id', 'strategy']) === strategyId).slice(0, 4)
}

export function filterRecordsByStrategy(records: RecordItem[], strategyId: string | null) {
  if (!strategyId) {
    return []
  }
  return records.filter((record) => pickText(record.payload, ['strategyId', 'strategy_id', 'strategy']) === strategyId)
}
