import { CandlestickChart, Play, RefreshCw, SquareTerminal } from 'lucide-react'
import { Link } from '@tanstack/react-router'
import { useMemo } from 'react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Panel } from '../../components/ui/panel'
import { useAgentAction } from '../../platform/agents/mutations'
import { useAgentWorkspaceSummary } from '../../platform/agents/queries'
import { cn } from '../../platform/core/utils'
import { buildRunBatches, buildStrategyOptions, buildStrategySnapshots, formatBrowserDateTime } from './trader-helpers'

function CompactFieldList({
  items,
}: {
  items: Array<{ label: string; value: string }>
}) {
  return (
    <div className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
      {items.map((item) => (
        <div
          key={item.label}
          className="grid gap-1 px-4 py-3 sm:grid-cols-[96px_minmax(0,1fr)] sm:items-center sm:gap-4"
        >
          <div className="text-xs text-slate-500">{item.label}</div>
          <div className="min-w-0 break-all text-sm text-slate-900">{item.value}</div>
        </div>
      ))}
    </div>
  )
}

export function TraderHomePage() {
  const { data, isFetching, refetch } = useAgentWorkspaceSummary('trader')
  const runOnce = useAgentAction('trader', 'run-once')
  const start = useAgentAction('trader', 'start')
  const stop = useAgentAction('trader', 'stop')
  const strategySnapshots = useMemo(() => buildStrategySnapshots(data), [data])
  const strategyOptions = useMemo(() => buildStrategyOptions(data, strategySnapshots), [data, strategySnapshots])
  const runBatches = useMemo(() => buildRunBatches(data), [data])

  const actionError = runOnce.error?.message || start.error?.message || stop.error?.message

  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white px-6 py-5">
        <div className="text-sm text-slate-500">智能体 / 交易员</div>
        <div className="mt-3 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-950">交易员</h1>
            <p className="mt-1 text-sm text-slate-500">先看智能体整体状态，再从策略列表进入具体策略。</p>
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
            <Button variant="outline" onClick={() => void refetch()}>
              <RefreshCw className={cn('h-4 w-4', isFetching && 'animate-spin')} />
              {isFetching ? '刷新中...' : '刷新'}
            </Button>
          </div>
        </div>
      </div>

      <Panel title="运行概览" description="用一行紧凑状态看清智能体当前状态，不再使用大卡片平铺。">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <CompactFieldList
            items={[
              { label: '服务', value: data?.running ? '运行中' : '已停止' },
              { label: '券商', value: data?.broker || '-' },
              { label: '最近动作', value: data?.last_action || '-' },
              { label: '最近运行', value: formatBrowserDateTime(data?.last_run_at) },
            ]}
          />
          <CompactFieldList
            items={[
              { label: '启用策略', value: String(strategyOptions.filter((item) => item.enabled).length) },
              { label: '持仓', value: String(data?.active_position_quantity ?? 0) },
              { label: '最近请求', value: String(data?.last_cycle_request_count ?? 0) },
            ]}
          />
        </div>
      </Panel>

      <Panel title="最近运行批次" description="按 run_id 查看最近 run_once 的策略、信号和订单计划产物。">
        {runBatches.length ? (
          <div className="overflow-hidden rounded-lg border border-slate-200">
            <div className="hidden grid-cols-[minmax(0,1.25fr)_176px_72px_72px_88px_72px_72px] gap-4 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 lg:grid">
              <div>Run ID</div>
              <div>完成时间</div>
              <div>策略</div>
              <div>信号</div>
              <div>订单计划</div>
              <div>抑制</div>
              <div>错误</div>
            </div>
            <div className="divide-y divide-slate-200">
              {runBatches.map((run) => (
                <div key={run.runId} className="grid gap-3 px-4 py-3 text-sm lg:grid-cols-[minmax(0,1.25fr)_176px_72px_72px_88px_72px_72px] lg:items-center lg:gap-4">
                  <div className="min-w-0 break-all font-medium text-slate-900">{run.runId}</div>
                  <div className="text-slate-600">{formatBrowserDateTime(run.finishedAt ?? run.startedAt)}</div>
                  <div className="text-slate-900">{run.strategyCount}</div>
                  <div className="text-slate-900">{run.signalCount}</div>
                  <div className="text-slate-900">{run.orderPlanCount}</div>
                  <div className={run.suppressedOrderCount ? 'font-medium text-amber-700' : 'text-slate-900'}>{run.suppressedOrderCount}</div>
                  <div className={run.errorCount ? 'font-medium text-rose-700' : 'text-slate-900'}>{run.errorCount}</div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
            还没有运行批次。执行一次 Trader 后会生成 runtime/runs/&lt;run_id&gt;/summary.json。
          </div>
        )}
      </Panel>

      <Panel title="策略列表" description="选择策略进入详情页，再看状态、记录和配置。">
        <div className="overflow-hidden rounded-lg border border-slate-200">
          <div className="hidden grid-cols-[minmax(0,1.2fr)_minmax(0,0.9fr)_112px_148px_104px] gap-4 border-b border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 lg:grid">
            <div>策略</div>
            <div>最近观察</div>
            <div>最近动作</div>
            <div>持仓</div>
            <div>操作</div>
          </div>
          {strategyOptions.length ? (
            <div className="divide-y divide-slate-200">
              {strategyOptions.map((strategy) => (
                <div key={strategy.id} className="px-4 py-4">
                  <div className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,0.9fr)_112px_148px_104px] lg:items-center lg:gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <CandlestickChart className="h-4 w-4 text-slate-400" />
                        <div className="text-sm font-medium text-slate-950">{strategy.name}</div>
                        <Badge variant={strategy.enabled ? 'success' : 'muted'}>
                          {strategy.enabled ? '启用中' : '已停用'}
                        </Badge>
                      </div>
                      <div className="mt-1 text-sm text-slate-600">
                        {strategy.id} · {strategy.symbol}
                      </div>
                    </div>
                    <div className="text-sm text-slate-600">{formatBrowserDateTime(strategy.observedAt)}</div>
                    <div className="text-sm text-slate-900">{strategy.lastAction}</div>
                    <div className="text-sm text-slate-900">{strategy.positionQuantity}</div>
                    <div>
                      <Link
                        to="/agents/trader/$strategyId"
                        params={{ strategyId: strategy.id }}
                        className="text-sm font-medium text-sky-700 hover:text-sky-800"
                      >
                        查看
                      </Link>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="p-4 text-sm text-slate-500">
              还没有策略配置。先在配置中新增策略。
            </div>
          )}
        </div>
      </Panel>

      {actionError ? (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">{actionError}</div>
      ) : null}
    </div>
  )
}
