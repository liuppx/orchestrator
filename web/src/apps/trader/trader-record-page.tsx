import { Link, useParams } from '@tanstack/react-router'
import { useMemo } from 'react'

import { Badge } from '../../components/ui/badge'
import { Panel } from '../../components/ui/panel'
import { useAgentWorkspaceSummary } from '../../platform/agents/queries'
import {
  buildRecordContextFields,
  buildRecordDetailFields,
  buildRecordItems,
  buildStrategySnapshots,
  formatBrowserDateTime,
  pickRecordStrategyId,
} from './trader-helpers'

function CompactFieldList({
  items,
}: {
  items: Array<{ label: string; value: string }>
}) {
  return (
    <div className="divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
      {items.map((item) => (
        <div
          key={item.label}
          className="grid gap-1 px-4 py-3 sm:grid-cols-[120px_minmax(0,1fr)] sm:items-center sm:gap-4"
        >
          <div className="text-xs text-slate-500">{item.label}</div>
          <div className="min-w-0 break-all text-sm text-slate-900">{item.value}</div>
        </div>
      ))}
    </div>
  )
}

export function TraderRecordPage() {
  const params = useParams({ strict: false }) as { strategyId?: string; recordId?: string }
  const strategyId = params.strategyId ?? null
  const recordId = params.recordId ? decodeURIComponent(params.recordId) : null
  const { data } = useAgentWorkspaceSummary('trader')
  const records = useMemo(() => buildRecordItems(data), [data])
  const record = useMemo(() => records.find((item) => item.id === recordId) ?? null, [recordId, records])
  const strategySnapshots = useMemo(() => buildStrategySnapshots(data), [data])
  const selectedRecordStrategyId = useMemo(() => pickRecordStrategyId(record), [record])
  const selectedRecordStrategy = useMemo(
    () => strategySnapshots.find((item) => item.id === selectedRecordStrategyId) ?? null,
    [selectedRecordStrategyId, strategySnapshots],
  )
  const detailFields = useMemo(() => (record ? buildRecordDetailFields(record) : []), [record])
  const contextFields = useMemo(() => (record ? buildRecordContextFields(record) : []), [record])

  return (
    <div className="space-y-6">
      <div className="rounded-md border border-slate-200 bg-white px-6 py-5">
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <Link to="/agents">智能体</Link>
          <span>/</span>
          <Link to="/agents/trader">交易员</Link>
          <span>/</span>
          <Link to="/agents/trader/$strategyId" params={{ strategyId: strategyId ?? '' }}>
            {strategyId ?? '策略'}
          </Link>
          <span>/</span>
          <span className="text-slate-900">记录详情</span>
        </div>
        <div className="mt-3">
          <h1 className="text-2xl font-semibold text-slate-950">记录详情</h1>
          <p className="mt-1 text-sm text-slate-500">这里单独展示一条记录，不再把详情强行塞进大工作台。</p>
        </div>
      </div>

      <Panel title="记录" description="先看关键信息，再看上下文和原始 JSON。">
        {record ? (
          <div className="space-y-4">
            <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="text-base font-semibold text-slate-950">{record.title}</div>
                    <Badge variant={record.kind === 'order' ? 'default' : 'muted'}>
                      {record.kind === 'order' ? '订单' : '信号'}
                    </Badge>
                  </div>
                  <div className="mt-1 text-sm text-slate-600">{record.summary}</div>
                </div>
                <div className="text-xs text-slate-500">{formatBrowserDateTime(record.timestamp)}</div>
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
              <div className="space-y-4">
                <div>
                  <div className="mb-3 text-sm font-medium text-slate-950">关键信息</div>
                  <CompactFieldList items={detailFields} />
                </div>
                <div className="rounded-md border border-slate-200 p-4">
                  <div className="text-sm font-medium text-slate-950">上下文字段</div>
                  {contextFields.length ? (
                    <div className="mt-3">
                      <CompactFieldList items={contextFields} />
                    </div>
                  ) : (
                    <div className="mt-3 text-sm text-slate-500">当前记录没有额外上下文字段。</div>
                  )}
                </div>
              </div>

              <div className="space-y-4">
                <div className="rounded-md border border-slate-200 p-4">
                  <div className="text-sm font-medium text-slate-950">关联策略</div>
                  {selectedRecordStrategy ? (
                    <div className="mt-3 space-y-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <div className="text-sm font-medium text-slate-900">{selectedRecordStrategy.name}</div>
                        <Badge variant={selectedRecordStrategy.enabled ? 'success' : 'muted'}>
                          {selectedRecordStrategy.enabled ? '启用中' : '已停用'}
                        </Badge>
                      </div>
                      <CompactFieldList
                        items={[
                          { label: '策略 ID', value: selectedRecordStrategy.id },
                          { label: '标的', value: selectedRecordStrategy.symbol },
                          { label: '策略类型', value: selectedRecordStrategy.strategy },
                          { label: '最近动作', value: selectedRecordStrategy.lastAction },
                        ]}
                      />
                    </div>
                  ) : (
                    <div className="mt-3 text-sm text-slate-500">当前记录没有匹配到策略摘要。</div>
                  )}
                </div>
                <details className="rounded-md border border-slate-200 bg-slate-950 p-4">
                  <summary className="cursor-pointer list-none text-sm font-medium text-slate-100">
                    原始记录 JSON
                  </summary>
                  <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-xs text-slate-100">
                    {JSON.stringify(record.payload, null, 2)}
                  </pre>
                </details>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-sm text-slate-500">未找到该记录。</div>
        )}
      </Panel>
    </div>
  )
}
