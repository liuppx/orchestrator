import { Link, useNavigate, useParams } from '@tanstack/react-router'
import { Activity, LockOpen, Play, RefreshCw, Save, SquareTerminal } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { Panel } from '../../components/ui/panel'
import { Select } from '../../components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui/tabs'
import { useAgentAction, useAgentConfigUpdate, useAgentOrderUnlock } from '../../platform/agents/mutations'
import { useAgentWorkspaceSummary } from '../../platform/agents/queries'
import { cn } from '../../platform/core/utils'
import {
  buildCandidateReviewSummary,
  buildConfigDefaults,
  buildCopiedStrategyDefaults,
  buildMarketObservation,
  buildNewStrategyDefaults,
  buildOrderLedgerDetailFields,
  buildOrderLedgerEntries,
  buildRecordItems,
  buildRecentStrategyRecords,
  buildRunBatches,
  buildStrategyOptions,
  buildStrategySnapshots,
  buildTraderDiagnostics,
  buildWatchlistCandidates,
  filterRecordsByStrategy,
  formatBrowserDateTime,
  normalizeStrategyPayload,
} from './trader-helpers'
import { ConfigField, StrategyBooleanToggles } from './trader-form-components'
import type { OrderLedgerEntry, RecordFilter, TraderConfigForm } from './trader-helpers'

function CompactFieldList({
  items,
}: {
  items: Array<{ label: string; value: string; valueClassName?: string }>
}) {
  return (
    <div className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
      {items.map((item) => (
        <div
          key={item.label}
          className="grid gap-1 px-4 py-3 sm:grid-cols-[120px_minmax(0,1fr)] sm:items-center sm:gap-4"
        >
          <div className="text-xs text-slate-500">{item.label}</div>
          <div className={cn('min-w-0 break-all text-sm text-slate-900', item.valueClassName)}>{item.value}</div>
        </div>
      ))}
    </div>
  )
}

function RecordTable({
  strategyId,
  records,
}: {
  strategyId: string
  records: Array<{
    id: string
    kind: 'signal' | 'order'
    title: string
    summary: string
    timestamp: string
  }>
}) {
  return (
    <div className="overflow-hidden rounded-md border border-slate-200">
      <div className="hidden grid-cols-[168px_minmax(0,1fr)_96px_176px] gap-4 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 md:grid">
        <div>时间</div>
        <div>记录</div>
        <div>类型</div>
        <div>操作</div>
      </div>
      <div className="divide-y divide-slate-200">
        {records.map((record) => (
          <div key={record.id} className="px-4 py-3">
            <div className="grid gap-3 md:grid-cols-[168px_minmax(0,1fr)_96px_176px] md:items-center md:gap-4">
              <div className="text-xs text-slate-500 md:text-sm">{formatBrowserDateTime(record.timestamp)}</div>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-slate-900">{record.title}</div>
                <div className="mt-1 truncate text-sm text-slate-600">{record.summary}</div>
              </div>
              <div>
                <Badge variant={record.kind === 'order' ? 'default' : 'muted'}>
                  {record.kind === 'order' ? '订单' : '信号'}
                </Badge>
              </div>
              <div>
                <Link
                  to="/agents/trader/$strategyId/records/$recordId"
                  params={{ strategyId, recordId: record.id }}
                  className="text-sm font-medium text-emerald-700 hover:text-emerald-800"
                >
                  查看详情
                </Link>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function TraderStrategyPage() {
  const { strategyId } = useParams({ strict: false }) as { strategyId?: string }
  const navigate = useNavigate()
  const { data, isLoading, isFetching, refetch } = useAgentWorkspaceSummary('trader')
  const runOnce = useAgentAction('trader', 'run-once')
  const start = useAgentAction('trader', 'start')
  const stop = useAgentAction('trader', 'stop')
  const diagnose = useAgentAction('trader', 'diagnose')
  const initConfig = useAgentAction('trader', 'init-config')
  const saveConfig = useAgentConfigUpdate('trader')
  const unlockOrder = useAgentOrderUnlock('trader')
  const strategySnapshots = useMemo(() => buildStrategySnapshots(data), [data])
  const strategyOptions = useMemo(() => buildStrategyOptions(data, strategySnapshots), [data, strategySnapshots])
  const runBatches = useMemo(() => buildRunBatches(data), [data])
  const diagnostics = useMemo(() => buildTraderDiagnostics(data), [data])
  const marketObservation = useMemo(() => buildMarketObservation(data), [data])
  const candidateReview = useMemo(() => buildCandidateReviewSummary(data), [data])
  const watchlistCandidates = useMemo(() => buildWatchlistCandidates(data), [data])
  const ledgerEntries = useMemo(() => buildOrderLedgerEntries(data), [data])
  const effectiveStrategyId = strategyId ?? strategyOptions[0]?.id ?? null
  const selectedStrategySnapshot = useMemo(
    () => strategySnapshots.find((item) => item.id === effectiveStrategyId) ?? null,
    [effectiveStrategyId, strategySnapshots],
  )
  const selectedStrategyOption = useMemo(
    () => strategyOptions.find((item) => item.id === effectiveStrategyId) ?? null,
    [effectiveStrategyId, strategyOptions],
  )
  const [draftStrategy, setDraftStrategy] = useState<TraderConfigForm | null>(null)
  const [tab, setTab] = useState('status')
  const [recordFilter, setRecordFilter] = useState<RecordFilter>('all')
  const [selectedLedgerKey, setSelectedLedgerKey] = useState<string | null>(null)
  const records = useMemo(() => buildRecordItems(data), [data])
  const currentStrategyRecords = useMemo(() => filterRecordsByStrategy(records, effectiveStrategyId), [effectiveStrategyId, records])
  const filteredCurrentStrategyRecords = useMemo(() => {
    if (recordFilter === 'all') {
      return currentStrategyRecords
    }
    return currentStrategyRecords.filter((item) => item.kind === recordFilter)
  }, [currentStrategyRecords, recordFilter])
  const recentSelectedStrategyRecords = useMemo(
    () => buildRecentStrategyRecords(records, effectiveStrategyId),
    [effectiveStrategyId, records],
  )
  const currentStrategyLedgerEntries = useMemo(
    () => ledgerEntries.filter((item) => item.strategyId === effectiveStrategyId),
    [effectiveStrategyId, ledgerEntries],
  )
  const selectedLedgerEntry = useMemo(
    () =>
      currentStrategyLedgerEntries.find((item) => `${item.tradingDay}-${item.orderKey}` === selectedLedgerKey) ??
      currentStrategyLedgerEntries[0] ??
      null,
    [currentStrategyLedgerEntries, selectedLedgerKey],
  )
  const selectedLedgerDetailFields = useMemo(
    () => (selectedLedgerEntry ? buildOrderLedgerDetailFields(selectedLedgerEntry) : []),
    [selectedLedgerEntry],
  )

  useEffect(() => {
    if (!strategyId && strategyOptions[0]) {
      void navigate({ to: '/agents/trader/$strategyId', params: { strategyId: strategyOptions[0].id }, replace: true })
    }
  }, [navigate, strategyId, strategyOptions])

  const defaults = useMemo(
    () => draftStrategy ?? buildConfigDefaults(data, effectiveStrategyId),
    [data, draftStrategy, effectiveStrategyId],
  )
  const form = useForm<TraderConfigForm>({ defaultValues: defaults })

  useEffect(() => {
    form.reset(defaults)
  }, [defaults, form])

  function createStrategyDraft() {
    setDraftStrategy(buildNewStrategyDefaults(data?.broker ?? 'paper'))
    setTab('config')
  }

  function duplicateCurrentStrategy() {
    setDraftStrategy(buildCopiedStrategyDefaults(form.getValues()))
    setTab('config')
  }

  async function toggleStrategyEnabled() {
    if (!selectedStrategyOption || saveConfig.isPending) {
      return
    }
    await saveConfig.mutateAsync({
      broker: data?.broker ?? 'paper',
      strategy_id: selectedStrategyOption.id,
      strategy: normalizeStrategyPayload(selectedStrategyOption.payload, !selectedStrategyOption.enabled),
    })
  }

  async function onSubmit(values: TraderConfigForm) {
    await saveConfig.mutateAsync({
      broker: values.broker,
      strategy_id: draftStrategy ? null : effectiveStrategyId,
      strategy: values,
    })
    setDraftStrategy(null)
    if (values.id !== effectiveStrategyId) {
      await navigate({ to: '/agents/trader/$strategyId', params: { strategyId: values.id } })
    }
  }

  function unlockLedgerEntry(entry: (typeof currentStrategyLedgerEntries)[number]) {
    unlockOrder.mutate({
      trading_day: entry.tradingDay,
      strategy_id: entry.strategyId,
      symbol: entry.symbol,
      side: entry.side,
      reason: 'manual retry from hub',
      actor: 'hub',
    })
  }

  function ledgerHistory(entry: OrderLedgerEntry | null) {
    const history = entry?.payload.guard_history
    return Array.isArray(history) ? history.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object') : []
  }

  const actionError =
    runOnce.error?.message ||
    start.error?.message ||
    stop.error?.message ||
    diagnose.error?.message ||
    initConfig.error?.message ||
    saveConfig.error?.message ||
    unlockOrder.error?.message

  return (
    <div className="space-y-6">
      <div className="rounded-md border border-slate-200 bg-white px-6 py-5">
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <Link to="/agents">智能体</Link>
          <span>/</span>
          <Link to="/agents/trader">交易员</Link>
          <span>/</span>
          <span className="text-slate-900">{selectedStrategySnapshot?.name ?? effectiveStrategyId ?? '策略'}</span>
        </div>
        <div className="mt-3 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-950">{selectedStrategySnapshot?.name ?? '策略详情'}</h1>
            <p className="mt-1 text-sm text-slate-500">围绕单个策略查看状态、记录和配置。</p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => runOnce.mutate()} disabled={runOnce.isPending}>
              <Play className="h-4 w-4" />
              {runOnce.isPending ? '执行中...' : '执行一次'}
            </Button>
            <Button variant="outline" onClick={() => start.mutate()} disabled={start.isPending}>
              <Play className="h-4 w-4" />
              {start.isPending ? '启动中...' : '启动'}
            </Button>
            <Button variant="outline" onClick={() => stop.mutate()} disabled={stop.isPending}>
              <SquareTerminal className="h-4 w-4" />
              {stop.isPending ? '停止中...' : '停止'}
            </Button>
            <Button variant="outline" onClick={() => diagnose.mutate()} disabled={diagnose.isPending}>
              <Activity className="h-4 w-4" />
              {diagnose.isPending ? '诊断中...' : '诊断'}
            </Button>
            <Button variant="outline" onClick={() => void refetch()}>
              <RefreshCw className={cn('h-4 w-4', isFetching && 'animate-spin')} />
              {isFetching ? '刷新中...' : '刷新'}
            </Button>
          </div>
        </div>
      </div>

      <Panel title="当前策略" description="这里不再重复放大一排大卡片，只保留单策略当前最关键的信息。">
        {selectedStrategySnapshot ? (
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
            <CompactFieldList
              items={[
                { label: '策略名称', value: selectedStrategySnapshot.name },
                { label: '策略 ID', value: selectedStrategySnapshot.id },
                { label: '标的', value: selectedStrategySnapshot.symbol },
                { label: '策略类型', value: selectedStrategySnapshot.strategy },
                { label: '周期', value: selectedStrategySnapshot.timeframe },
                { label: '最近观察', value: formatBrowserDateTime(selectedStrategySnapshot.observedAt) },
                {
                  label: '最新价格',
                  value:
                    selectedStrategySnapshot.latestPrice == null ? '-' : selectedStrategySnapshot.latestPrice.toFixed(3),
                },
              ]}
            />
            <div className="space-y-3">
              <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3">
                <div className="text-xs text-slate-500">状态</div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Badge variant={selectedStrategySnapshot.enabled ? 'success' : 'muted'}>
                    {selectedStrategySnapshot.enabled ? '启用中' : '已停用'}
                  </Badge>
                  <Badge
                    variant={
                      selectedStrategySnapshot.lastAction === 'buy' || selectedStrategySnapshot.lastAction === 'sell'
                        ? 'default'
                        : 'muted'
                    }
                  >
                    {selectedStrategySnapshot.lastAction}
                  </Badge>
                </div>
              </div>
              <CompactFieldList
                items={[
                  { label: '持仓数量', value: String(selectedStrategySnapshot.positionQuantity) },
                  {
                    label: '风险结果',
                    value:
                      selectedStrategySnapshot.riskOk == null ? '-' : selectedStrategySnapshot.riskOk ? '通过' : '未通过',
                  },
                  { label: '风险说明', value: selectedStrategySnapshot.riskReason ?? '-' },
                ]}
              />
            </div>
          </div>
        ) : (
          <div className="text-sm text-slate-500">{isLoading ? '正在读取策略...' : '未找到该策略。'}</div>
        )}
      </Panel>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="status">状态</TabsTrigger>
          <TabsTrigger value="records">记录</TabsTrigger>
          <TabsTrigger value="config">配置</TabsTrigger>
        </TabsList>

        <TabsContent value="status">
          <Panel
            title="运行诊断"
            description="启动前检查配置、数据源、broker 和 runtime。诊断结果只显示凭证是否已配置，不显示 token。"
          >
            {diagnostics ? (
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={diagnostics.ok ? 'success' : 'muted'}>
                      {diagnostics.ok ? '可运行' : '有阻塞'}
                    </Badge>
                    <Badge variant={diagnostics.brokerSupported ? 'success' : 'muted'}>
                      {diagnostics.brokerProvider}
                    </Badge>
                    <Badge variant={diagnostics.accessTokenConfigured ? 'success' : 'muted'}>
                      access token {diagnostics.accessTokenConfigured ? '已配置' : '缺失'}
                    </Badge>
                    <Badge variant={diagnostics.refreshTokenConfigured ? 'success' : 'muted'}>
                      refresh token {diagnostics.refreshTokenConfigured ? '已配置' : '缺失'}
                    </Badge>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={initConfig.isPending}
                      onClick={() => initConfig.mutate()}
                    >
                      <Save className="h-4 w-4" />
                      {initConfig.isPending ? '初始化中...' : '初始化配置'}
                    </Button>
                  </div>
                  {diagnostics.errors.length ? (
                    <div className="rounded-md border border-rose-200 bg-rose-50 p-4">
                      <div className="text-sm font-medium text-rose-900">阻塞项</div>
                      <ul className="mt-2 space-y-1 text-sm text-rose-700">
                        {diagnostics.errors.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  {diagnostics.warnings.length ? (
                    <div className="rounded-md border border-amber-200 bg-amber-50 p-4">
                      <div className="text-sm font-medium text-amber-900">提醒</div>
                      <ul className="mt-2 space-y-1 text-sm text-amber-700">
                        {diagnostics.warnings.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  {!diagnostics.errors.length && !diagnostics.warnings.length ? (
                    <div className="rounded-md border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-700">
                      当前诊断没有发现阻塞项或提醒。
                    </div>
                  ) : null}
                </div>
                <CompactFieldList
                  items={[
                    { label: '策略总数', value: String(diagnostics.strategyCount) },
                    { label: '启用策略', value: String(diagnostics.enabledStrategyCount) },
                    { label: '停用策略', value: String(diagnostics.disabledStrategyCount) },
                    { label: 'Broker 配置', value: diagnostics.brokerConfigured == null ? '-' : diagnostics.brokerConfigured ? '已配置' : '未配置' },
                    { label: '数据源', value: diagnostics.dataProvider },
                    { label: '数据地址', value: diagnostics.dataBaseUrl },
                    { label: 'env 文件', value: diagnostics.envFileExists ? '存在' : '缺失' },
                    { label: 'runtime', value: diagnostics.runtimeDirExists ? '存在' : '待创建' },
                    { label: 'state 文件', value: diagnostics.stateFileExists ? '存在' : '未生成' },
                    { label: '策略文件', value: diagnostics.strategyFile },
                  ]}
                />
              </div>
            ) : (
              <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
                还没有诊断数据。
              </div>
            )}
          </Panel>

          <Panel
            title="市场观察与候选池"
            description="把已配置观察标的按强弱和触发结果分成 A/B/C，先用于复盘和人工判断。"
          >
            {marketObservation ? (
              <div className="space-y-4">
                <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
                  <CompactFieldList
                    items={[
                      { label: '交易许可', value: marketObservation.tradePermission },
                      { label: '候选数量', value: String(marketObservation.candidateCount) },
                      { label: 'A 级', value: String(marketObservation.gradeCounts.A) },
                      { label: 'B 级', value: String(marketObservation.gradeCounts.B) },
                      { label: 'C 级', value: String(marketObservation.gradeCounts.C) },
                      { label: '错误数', value: String(marketObservation.errorCount) },
                    ]}
                  />
                  <div className="overflow-hidden rounded-md border border-slate-200">
                    <div className="hidden grid-cols-[64px_96px_minmax(0,1fr)_72px_88px_104px] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 lg:grid">
                      <div>等级</div>
                      <div>标的</div>
                      <div>理由</div>
                      <div>分数</div>
                      <div>信号</div>
                      <div>许可</div>
                    </div>
                    <div className="divide-y divide-slate-200">
                      {watchlistCandidates.length ? (
                        watchlistCandidates.map((candidate) => (
                          <div
                            key={`${candidate.strategyId}-${candidate.symbol}`}
                            className="grid gap-3 px-4 py-3 text-sm lg:grid-cols-[64px_96px_minmax(0,1fr)_72px_88px_104px] lg:items-center"
                          >
                            <div>
                              <Badge variant={candidate.grade === 'A' ? 'success' : candidate.grade === 'B' ? 'default' : 'muted'}>
                                {candidate.grade}
                              </Badge>
                            </div>
                            <div className="min-w-0">
                              <div className="font-medium text-slate-900">{candidate.symbol}</div>
                              <div className="mt-1 truncate text-xs text-slate-500">{candidate.name}</div>
                            </div>
                            <div className="min-w-0 text-slate-600">
                              <div className="truncate">{candidate.reasons[0] ?? candidate.signalReason}</div>
                              {candidate.riskFlags.length ? (
                                <div className="mt-1 truncate text-xs text-amber-700">{candidate.riskFlags[0]}</div>
                              ) : null}
                            </div>
                            <div className="text-slate-900">{candidate.score}</div>
                            <div className="text-slate-900">{candidate.signalAction}</div>
                            <div className="text-slate-900">{candidate.tradePermission}</div>
                          </div>
                        ))
                      ) : (
                        <div className="px-4 py-4 text-sm text-slate-500">最近批次还没有候选池记录。</div>
                      )}
                    </div>
                  </div>
                </div>
                {candidateReview ? (
                  <div className="rounded-md border border-slate-200 bg-slate-50 p-4">
                    <div className="text-sm font-medium text-slate-950">候选池复盘</div>
                    <div className="mt-3 grid gap-4 md:grid-cols-4">
                      <div>
                        <div className="text-xs text-slate-500">复盘数量</div>
                        <div className="mt-1 text-lg font-semibold text-slate-950">{candidateReview.reviewCount}</div>
                      </div>
                      <div>
                        <div className="text-xs text-slate-500">正收益</div>
                        <div className="mt-1 text-lg font-semibold text-emerald-700">{candidateReview.positiveCount}</div>
                      </div>
                      <div>
                        <div className="text-xs text-slate-500">失败</div>
                        <div className="mt-1 text-lg font-semibold text-rose-700">{candidateReview.failedCount}</div>
                      </div>
                      <div>
                        <div className="text-xs text-slate-500">平均收益</div>
                        <div className="mt-1 text-lg font-semibold text-slate-950">
                          {candidateReview.averageReturnPct == null ? '-' : `${candidateReview.averageReturnPct.toFixed(2)}%`}
                        </div>
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
                运行一次后会生成市场观察和候选池。
              </div>
            )}
          </Panel>

          <div className="grid gap-4 xl:grid-cols-[0.95fr_1.05fr]">
            <Panel title="运行摘要" description="这里保留全局运行摘要，帮助判断本轮 run_once 的范围和结果。">
              <CompactFieldList
                items={[
                  { label: '最近执行动作', value: data?.last_action || '-' },
                  { label: '最近运行时间', value: formatBrowserDateTime(data?.last_run_at) },
                  { label: '最近评估策略', value: String(data?.last_cycle_strategy_count ?? 0) },
                  { label: '最近数据请求', value: String(data?.last_cycle_request_count ?? 0) },
                  { label: '最近批次数', value: String(runBatches.length) },
                  { label: '最近信号', value: formatBrowserDateTime(data?.last_signal_at) },
                  { label: '最近订单', value: formatBrowserDateTime(data?.last_order_at) },
                ]}
              />
              {runBatches.length ? (
                <div className="mt-4 overflow-hidden rounded-md border border-slate-200">
                  <div className="grid grid-cols-[minmax(0,1fr)_64px_80px_64px_64px] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500">
                    <div>最近批次</div>
                    <div>策略</div>
                    <div>订单计划</div>
                    <div>抑制</div>
                    <div>错误</div>
                  </div>
                  <div className="divide-y divide-slate-200">
                    {runBatches.slice(0, 4).map((run) => (
                      <div key={run.runId} className="grid grid-cols-[minmax(0,1fr)_64px_80px_64px_64px] gap-3 px-4 py-3 text-sm">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-slate-900">{run.runId}</div>
                          <div className="mt-1 text-xs text-slate-500">{formatBrowserDateTime(run.finishedAt ?? run.startedAt)}</div>
                        </div>
                        <div className="text-slate-900">{run.strategyCount}</div>
                        <div className="text-slate-900">{run.orderPlanCount}</div>
                        <div className={run.suppressedOrderCount ? 'font-medium text-amber-700' : 'text-slate-900'}>{run.suppressedOrderCount}</div>
                        <div className={run.errorCount ? 'font-medium text-rose-700' : 'text-slate-900'}>{run.errorCount}</div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </Panel>

            <Panel title="当前策略影响" description="固定聚焦当前策略，而不是在大工作台里和其他策略混看。">
              {selectedStrategySnapshot ? (
                <div className="space-y-4">
                  <CompactFieldList
                    items={[
                      { label: '最近动作', value: selectedStrategySnapshot.lastAction },
                      { label: '最近观察', value: formatBrowserDateTime(selectedStrategySnapshot.observedAt) },
                      { label: '持仓数量', value: String(selectedStrategySnapshot.positionQuantity) },
                      { label: '关联记录', value: String(currentStrategyRecords.length) },
                      { label: '最近结论', value: selectedStrategySnapshot.lastReason },
                    ]}
                  />
                  <div>
                    <div className="text-sm font-medium text-slate-950">最近记录</div>
                    {recentSelectedStrategyRecords.length ? (
                      <div className="mt-3">
                        <RecordTable strategyId={effectiveStrategyId ?? ''} records={recentSelectedStrategyRecords} />
                      </div>
                    ) : (
                      <div className="mt-3 text-sm text-slate-500">当前策略还没有最近记录。</div>
                    )}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-slate-500">未选择策略。</div>
              )}
            </Panel>
          </div>

          <Panel title="订单防线" description="查看当前策略当日订单 ledger，必要时人工解锁后允许下一轮重试。">
            {currentStrategyLedgerEntries.length ? (
              <div className="overflow-hidden rounded-md border border-slate-200">
                <div className="hidden grid-cols-[112px_72px_72px_88px_88px_minmax(0,1fr)_176px] gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 lg:grid">
                  <div>交易日</div>
                  <div>方向</div>
                  <div>数量</div>
                  <div>状态</div>
                  <div>防线</div>
                  <div>记录时间</div>
                  <div>操作</div>
                </div>
                <div className="divide-y divide-slate-200">
                  {currentStrategyLedgerEntries.map((entry) => (
                    <div
                      key={`${entry.tradingDay}-${entry.orderKey}`}
                      className="grid gap-3 px-4 py-3 text-sm lg:grid-cols-[112px_72px_72px_88px_88px_minmax(0,1fr)_176px] lg:items-center"
                    >
                      <div className="font-medium text-slate-900">{entry.tradingDay}</div>
                      <div className="text-slate-900">{entry.side}</div>
                      <div className="text-slate-900">{entry.quantity}</div>
                      <div>
                        <Badge variant={entry.lifecycleStatus === 'filled' ? 'success' : 'muted'}>
                          {entry.lifecycleStatus}
                        </Badge>
                      </div>
                      <div>
                        <Badge variant={entry.guardActive ? 'default' : 'muted'}>
                          {entry.guardActive ? '锁定' : '已解锁'}
                        </Badge>
                      </div>
                      <div className="min-w-0 text-slate-600">
                        <div className="truncate">{formatBrowserDateTime(entry.recordedAt)}</div>
                        {entry.unlockReason !== '-' ? (
                          <div className="mt-1 truncate text-xs text-slate-500">{entry.unlockReason}</div>
                        ) : null}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => setSelectedLedgerKey(`${entry.tradingDay}-${entry.orderKey}`)}
                        >
                          详情
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={!entry.guardActive || unlockOrder.isPending}
                          onClick={() => unlockLedgerEntry(entry)}
                        >
                          <LockOpen className="h-4 w-4" />
                          解锁
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
                当前策略还没有订单防线记录。
              </div>
            )}
            {selectedLedgerEntry ? (
              <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
                <div className="rounded-md border border-slate-200 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium text-slate-950">Ledger 明细</div>
                      <div className="mt-1 text-xs text-slate-500">{selectedLedgerEntry.orderKey}</div>
                    </div>
                    <Badge variant={selectedLedgerEntry.guardActive ? 'default' : 'muted'}>
                      {selectedLedgerEntry.guardActive ? '锁定' : '已解锁'}
                    </Badge>
                  </div>
                  <div className="mt-3">
                    <CompactFieldList items={selectedLedgerDetailFields} />
                  </div>
                </div>
                <div className="space-y-4">
                  <div className="rounded-md border border-slate-200 p-4">
                    <div className="text-sm font-medium text-slate-950">解锁历史</div>
                    {ledgerHistory(selectedLedgerEntry).length ? (
                      <div className="mt-3 divide-y divide-slate-200 rounded-md border border-slate-200">
                        {ledgerHistory(selectedLedgerEntry).map((item, index) => (
                          <div key={`${item.ts ?? index}`} className="px-3 py-3 text-sm">
                            <div className="font-medium text-slate-900">{String(item.event ?? '-')}</div>
                            <div className="mt-1 text-xs text-slate-500">{formatBrowserDateTime(String(item.ts ?? ''))}</div>
                            <div className="mt-2 text-slate-600">{String(item.reason ?? '-')}</div>
                            <div className="mt-1 text-xs text-slate-500">actor: {String(item.actor ?? '-')}</div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-3 text-sm text-slate-500">当前 ledger 没有解锁历史。</div>
                    )}
                  </div>
                  <details className="rounded-md border border-slate-200 bg-slate-950 p-4">
                    <summary className="cursor-pointer list-none text-sm font-medium text-slate-100">
                      原始 Ledger JSON
                    </summary>
                    <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap text-xs text-slate-100">
                      {JSON.stringify(selectedLedgerEntry.payload, null, 2)}
                    </pre>
                  </details>
                </div>
              </div>
            ) : null}
          </Panel>
        </TabsContent>

        <TabsContent value="records">
          <Panel title="记录列表" description="先看当前策略的记录列表，再下钻到记录详情页。">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <Tabs value={recordFilter} onValueChange={(value) => setRecordFilter(value as RecordFilter)}>
                <TabsList className="h-10">
                  <TabsTrigger value="all" className="min-w-[72px]">
                    全部 {currentStrategyRecords.length}
                  </TabsTrigger>
                  <TabsTrigger value="signal" className="min-w-[72px]">
                    信号 {currentStrategyRecords.filter((item) => item.kind === 'signal').length}
                  </TabsTrigger>
                  <TabsTrigger value="order" className="min-w-[72px]">
                    订单 {currentStrategyRecords.filter((item) => item.kind === 'order').length}
                  </TabsTrigger>
                </TabsList>
              </Tabs>
              <div className="text-xs text-slate-500">当前筛选 {filteredCurrentStrategyRecords.length} 条</div>
            </div>
            <div className="space-y-3">
              {filteredCurrentStrategyRecords.length ? (
                <RecordTable strategyId={effectiveStrategyId ?? ''} records={filteredCurrentStrategyRecords} />
              ) : (
                <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
                  当前策略在所选筛选下没有记录。
                </div>
              )}
            </div>
          </Panel>
        </TabsContent>

        <TabsContent value="config">
          <Panel
            title="策略配置"
            description={draftStrategy ? '当前是新增草稿。保存后会作为新策略写入，不影响已有策略。' : '这里只编辑当前策略。'}
          >
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" onClick={createStrategyDraft}>
                  新建策略
                </Button>
                <Button type="button" size="sm" variant="outline" onClick={duplicateCurrentStrategy}>
                  复制当前
                </Button>
                {selectedStrategyOption ? (
                  <Button type="button" size="sm" variant="outline" onClick={() => void toggleStrategyEnabled()}>
                    {selectedStrategyOption.enabled ? '停用当前策略' : '启用当前策略'}
                  </Button>
                ) : null}
              </div>

              <div className="rounded-md border border-slate-200 p-4">
                <div className="text-sm font-medium text-slate-950">基础信息</div>
                <div className="mt-1 text-xs text-slate-500">先确认策略身份、标的和运行模式。</div>
                <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <ConfigField label="券商通道">
                    <Select {...form.register('broker')}>
                      <option value="paper">paper</option>
                      <option value="eastmoney_stub">eastmoney_stub</option>
                    </Select>
                  </ConfigField>
                  <ConfigField label="策略 ID">
                    <Input {...form.register('id', { required: true })} />
                  </ConfigField>
                  <ConfigField label="标的代码">
                    <Input {...form.register('symbol', { required: true })} />
                  </ConfigField>
                  <ConfigField label="标的名称">
                    <Input {...form.register('name')} />
                  </ConfigField>
                  <ConfigField label="策略类型">
                    <Select {...form.register('strategy')}>
                      <option value="breakout">breakout</option>
                      <option value="auction_wave">auction_wave</option>
                    </Select>
                  </ConfigField>
                  <ConfigField label="周期">
                    <Input {...form.register('timeframe')} />
                  </ConfigField>
                  <ConfigField label="市场">
                    <Input {...form.register('market')} />
                  </ConfigField>
                  <ConfigField label="观察窗口">
                    <Input type="number" {...form.register('history_window', { valueAsNumber: true })} />
                  </ConfigField>
                </div>
              </div>

              <div className="rounded-md border border-slate-200 p-4">
                <div className="text-sm font-medium text-slate-950">仓位与风控</div>
                <div className="mt-1 text-xs text-slate-500">这组参数决定下单规模和风险边界。</div>
                <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <ConfigField label="下单数量">
                    <Input type="number" {...form.register('quantity', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="持仓数量">
                    <Input type="number" {...form.register('position_quantity', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="最大仓位">
                    <Input type="number" {...form.register('max_position', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="止损比例">
                    <Input type="number" step="0.001" {...form.register('stop_loss_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="止盈比例">
                    <Input type="number" step="0.001" {...form.register('take_profit_pct', { valueAsNumber: true })} />
                  </ConfigField>
                </div>
              </div>

              <div className="rounded-md border border-slate-200 p-4">
                <div className="text-sm font-medium text-slate-950">高级策略参数</div>
                <div className="mt-1 text-xs text-slate-500">只在需要微调行为时修改，平时可以保持默认。</div>
                <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  <ConfigField label="突破回看">
                    <Input type="number" {...form.register('breakout_lookback', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="活跃回看天数">
                    <Input type="number" {...form.register('active_lookback_days', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="活跃阈值 %">
                    <Input type="number" step="0.1" {...form.register('active_threshold_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="突破缓冲 %">
                    <Input type="number" step="0.1" {...form.register('breakout_buffer_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="压力位缓冲 %">
                    <Input type="number" step="0.1" {...form.register('resistance_buffer_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="竞价锁定时间">
                    <Input {...form.register('auction_lock_time')} />
                  </ConfigField>
                  <ConfigField label="开盘处理结束">
                    <Input {...form.register('auction_open_end_time')} />
                  </ConfigField>
                  <ConfigField label="零轴阈值 %">
                    <Input type="number" step="0.1" {...form.register('auction_zero_threshold_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="强开阈值 %">
                    <Input type="number" step="0.1" {...form.register('auction_strong_open_threshold_pct', { valueAsNumber: true })} />
                  </ConfigField>
                  <ConfigField label="午后退出时间">
                    <Input {...form.register('afternoon_exit_time')} />
                  </ConfigField>
                  <ConfigField label="涨停阈值 %">
                    <Input type="number" step="0.1" {...form.register('limit_up_threshold_pct', { valueAsNumber: true })} />
                  </ConfigField>
                </div>
              </div>

              <StrategyBooleanToggles
                formWatch={(name) => form.watch(name) as boolean}
                formSetValue={(name, value) => form.setValue(name, value as never)}
              />

              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" disabled={saveConfig.isPending}>
                  <Save className="h-4 w-4" />
                  {saveConfig.isPending ? '保存中...' : draftStrategy ? '保存为新策略' : '保存配置'}
                </Button>
                <Button type="button" variant="outline" onClick={() => form.reset(defaults)}>
                  重置
                </Button>
                {draftStrategy ? (
                  <Button type="button" variant="outline" onClick={() => setDraftStrategy(null)}>
                    取消草稿
                  </Button>
                ) : null}
              </div>
            </form>
          </Panel>
        </TabsContent>
      </Tabs>

      {actionError ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">{actionError}</div>
      ) : null}
    </div>
  )
}
