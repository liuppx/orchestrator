import { useMemo, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { ArrowUpRight, CircleAlert, CircleCheck, CircleX, Clock3, Play, RefreshCw, Search, Server, Square, Terminal } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { useRuntimeHubs, useRuntimeInstanceDiagnose, useRuntimeInstanceLogs, useRuntimeInstances } from '../../platform/agents/queries'
import { useRuntimeInstanceAction } from '../../platform/agents/mutations'
import type { RuntimeInstance } from '../../platform/core/types'

type StatusFilter = 'all' | 'running' | 'stopped' | 'attention'

const EMPTY_INSTANCES: RuntimeInstance[] = []

function stateLabel(value: string) {
  const labels: Record<string, string> = {
    created: '已创建', starting: '启动中', running: '运行中', stopping: '停止中', stopped: '已停止', error: '异常', unknown: '未知',
  }
  return labels[value] ?? value
}

function hubStateLabel(value: string) {
  return ({ online: '已连接', offline: '已离线', unknown: '未知' } as Record<string, string>)[value] ?? value
}

function commandStateLabel(status: string | null) {
  return ({ pending: '等待 Hub', running: '执行中', succeeded: '已完成', failed: '失败', unknown: '结果未知', rejected: '已拒绝' } as Record<string, string>)[status ?? ''] ?? '无最近命令'
}

function matchesFilter(instance: RuntimeInstance, filter: StatusFilter) {
  if (filter === 'running') return instance.observed_state === 'running'
  if (filter === 'stopped') return ['created', 'stopped'].includes(instance.observed_state)
  if (filter === 'attention') return ['error', 'unknown'].includes(instance.observed_state)
  return true
}

export function RuntimePage() {
  const query = useRuntimeInstances()
  const hubsQuery = useRuntimeHubs()
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const instances = query.data?.items ?? EMPTY_INSTANCES
  const selected = instances.find((item) => item.id === selectedId) ?? instances[0] ?? null
  const logsQuery = useRuntimeInstanceLogs(selected?.id ?? null)
  const diagnoseQuery = useRuntimeInstanceDiagnose(selected?.id ?? null)
  const runtimeAction = useRuntimeInstanceAction()
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return instances.filter((instance) => {
      const text = `${instance.name} ${instance.id} ${instance.runtime_type} ${instance.workload_type}`.toLowerCase()
      return matchesFilter(instance, filter) && (!needle || text.includes(needle))
    })
  }, [filter, instances, search])

  return (
    <div className="control-page">
      <header className="page-heading">
        <div>
          <h1>运行时实例</h1>
          <p>查看机器连接、实例状态、最近命令和运行日志。</p>
        </div>
        <div className="heading-actions">
          <Button variant="outline" onClick={() => void query.refetch()} aria-label="刷新实例列表">
            <RefreshCw className={query.isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
            {query.isFetching ? '正在更新' : '刷新列表'}
          </Button>
          <Link to="/agents/messenger" className="primary-action"><Play className="h-4 w-4" />创建实例</Link>
        </div>
      </header>

      <section className="metric-strip compact-metrics" aria-label="实例统计">
        <div className="metric-cell"><span>全部</span><strong>{query.data?.total ?? '—'}</strong></div>
        <div className="metric-cell metric-good"><span>运行中</span><strong>{query.data?.running ?? '—'}</strong></div>
        <div className="metric-cell"><span>已停止</span><strong>{query.data?.stopped ?? '—'}</strong></div>
        <div className="metric-cell metric-attention"><span>需要关注</span><strong>{query.data?.attention ?? '—'}</strong></div>
      </section>

      <section className="surface-section hub-surface" aria-labelledby="hub-status-heading">
        <div className="section-heading">
          <div>
            <h2 id="hub-status-heading">机器 Hub</h2>
            <p>实例所在机器的连接状态和本地 Adapter 能力。</p>
          </div>
          <span className="hub-summary">{hubsQuery.data?.online ?? '—'} 台在线</span>
        </div>
        {hubsQuery.isLoading ? <div className="empty-inline">正在读取机器状态…</div> : null}
        {hubsQuery.error ? <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />{hubsQuery.error.message}<Button variant="outline" size="sm" onClick={() => void hubsQuery.refetch()}>重试</Button></div> : null}
        {!hubsQuery.isLoading && !hubsQuery.error && hubsQuery.data?.items.length === 0 ? <div className="empty-inline">当前钱包还没有关联的机器 Hub。</div> : null}
        {hubsQuery.data?.items.map((hub) => (
          <div className="hub-row" key={hub.id}>
            <span className={`hub-state-mark hub-state-${hub.status}`} aria-hidden="true" />
            <div className="hub-row-copy"><strong>{hub.id}</strong><span>{hub.adapter_count} 个 Adapter · {hub.instance_count} 个实例</span></div>
            <span className={`hub-state-label hub-state-label-${hub.status}`}>{hubStateLabel(hub.status)}</span>
            <span className="hub-seen">{hub.last_seen_at ? new Date(hub.last_seen_at).toLocaleTimeString() : '未上报'}</span>
          </div>
        ))}
      </section>

      <section className="surface-section runtime-surface">
        <div className="runtime-toolbar">
          <div className="segmented" role="group" aria-label="按状态筛选">
            {([
              ['all', '全部'], ['running', '运行中'], ['stopped', '已停止'], ['attention', '需要关注'],
            ] as const).map(([value, label]) => (
              <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>
            ))}
          </div>
          <label className="search-control">
            <Search className="h-4 w-4" />
            <Input aria-label="搜索实例" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、类型或 ID" />
          </label>
        </div>

        {runtimeAction.error ? <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />操作失败：{runtimeAction.error.message}</div> : null}
        {query.error ? (
          <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />{query.error.message}<Button variant="outline" size="sm" onClick={() => void query.refetch()}>重试</Button></div>
        ) : null}
        <div className="table-scroll">
          <table className="runtime-table">
            <thead><tr><th>实例</th><th>机器 / 命令</th><th>Runtime</th><th>工作负载</th><th>期望 / 实际</th><th>模型</th><th>本机地址</th><th>操作</th></tr></thead>
            <tbody>
              {query.isLoading ? <tr><td colSpan={8} className="table-message">正在读取实例…</td></tr> : null}
              {!query.isLoading && !query.error && filtered.length === 0 ? (
                <tr><td colSpan={8} className="table-message">{search || filter !== 'all' ? '没有符合条件的实例。' : '还没有实例。'} <Link to="/agents/messenger" className="text-link">创建 OpenClaw 实例 <ArrowUpRight className="h-4 w-4" /></Link></td></tr>
              ) : null}
              {filtered.map((instance) => (
                <tr key={instance.id} className={selected?.id === instance.id ? 'selected-row' : ''}>
                  <td><button className="instance-name" type="button" onClick={() => setSelectedId(instance.id)}>{instance.name}<small>{instance.id}</small></button></td>
                  <td><div className="hub-cell"><strong>{instance.hub_id ?? '未分配'}</strong><span><CommandIcon status={instance.latest_command_status} />{commandStateLabel(instance.latest_command_status)}</span></div></td>
                  <td>{instance.runtime_type}</td>
                  <td>{instance.workload_type}</td>
                  <td><div className="state-pair"><span>{stateLabel(instance.desired_state)}</span><Badge variant={instance.observed_state === 'running' ? 'success' : ['error', 'unknown'].includes(instance.observed_state) ? 'danger' : 'muted'}>{stateLabel(instance.observed_state)}</Badge></div></td>
                  <td className="model-cell">{instance.model || '—'}</td>
                  <td><code>{instance.endpoint.replace('http://', '')}</code></td>
                  <td>
                    {instance.observed_state === 'running' ? (
                      <Button variant="outline" size="sm" aria-label={`停止 ${instance.name}`} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'stop' })}><Square className="h-3.5 w-3.5" />停止</Button>
                    ) : (
                      <Button variant="outline" size="sm" aria-label={`启动 ${instance.name}`} disabled={runtimeAction.isPending || ['starting', 'stopping'].includes(instance.observed_state)} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'start' })}><Play className="h-3.5 w-3.5" />启动</Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="runtime-mobile-list" role="list" aria-label="运行时实例">
          {query.isLoading ? <div className="empty-inline">正在读取实例…</div> : null}
          {!query.isLoading && !query.error && filtered.length === 0 ? (
            <div className="runtime-mobile-empty">
              <span>{search || filter !== 'all' ? '没有符合条件的实例。' : '还没有实例。'}</span>
              {!search && filter === 'all' ? <Link to="/agents/messenger" className="text-link">创建 OpenClaw 实例 <ArrowUpRight className="h-4 w-4" /></Link> : null}
            </div>
          ) : null}
          {filtered.map((instance) => (
            <article
              key={instance.id}
              role="listitem"
              className={'runtime-mobile-card' + (selected?.id === instance.id ? ' is-selected' : '')}
            >
              <div className="runtime-mobile-card-heading">
                <button
                  className="runtime-mobile-name"
                  type="button"
                  aria-pressed={selected?.id === instance.id}
                  onClick={() => setSelectedId(instance.id)}
                >
                  <strong>{instance.name}</strong>
                  <small>{instance.id}</small>
                </button>
                <Badge variant={instance.observed_state === 'running' ? 'success' : ['error', 'unknown'].includes(instance.observed_state) ? 'danger' : 'muted'}>
                  {stateLabel(instance.observed_state)}
                </Badge>
              </div>
              <div className="runtime-mobile-meta">
                <div><span>机器 Hub</span><strong>{instance.hub_id ?? '未分配'}</strong></div>
                <div><span>运行时</span><strong>{instance.runtime_type} / {instance.workload_type}</strong></div>
                <div><span>最近命令</span><strong><CommandIcon status={instance.latest_command_status} />{commandStateLabel(instance.latest_command_status)}</strong></div>
                <div><span>模型</span><strong>{instance.model || '—'}</strong></div>
              </div>
              <div className="runtime-mobile-card-footer">
                <code>{instance.endpoint.replace('http://', '')}</code>
                {instance.observed_state === 'running' ? (
                  <Button variant="outline" size="sm" aria-label={'停止 ' + instance.name} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'stop' })}><Square className="h-3.5 w-3.5" />停止</Button>
                ) : (
                  <Button variant="outline" size="sm" aria-label={'启动 ' + instance.name} disabled={runtimeAction.isPending || ['starting', 'stopping'].includes(instance.observed_state)} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'start' })}><Play className="h-3.5 w-3.5" />启动</Button>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>

      {selected ? (
        <section className="surface-section detail-section" aria-labelledby="runtime-detail-heading">
          <div className="section-heading">
            <div><h2 id="runtime-detail-heading">{selected.name}</h2><p>{selected.id} · PID {selected.process_id ?? '—'} · 更新于 {new Date(selected.updated_at).toLocaleString()}</p></div>
            <Link to="/agents/messenger" className="text-link">打开消息工作台 <ArrowUpRight className="h-4 w-4" /></Link>
          </div>
          {selected.last_error ? <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />{selected.last_error}</div> : null}
          <div className="detail-grid">
            <div><span>期望状态</span><strong>{stateLabel(selected.desired_state)}</strong></div>
            <div><span>观测状态</span><strong>{stateLabel(selected.observed_state)}</strong></div>
            <div><span>机器 Hub</span><strong>{selected.hub_id ?? '未分配'} · {hubStateLabel(selected.hub_status)}</strong></div>
            <div><span>最近命令</span><strong>{commandStateLabel(selected.latest_command_status)}</strong></div>
            <div><span>本机地址</span><code>{selected.endpoint}</code></div>
            <div><span>诊断建议</span><strong>{diagnoseQuery.data?.recommended_action || (diagnoseQuery.isLoading ? '检查中…' : '暂无')}</strong></div>
          </div>
          <div className="log-heading"><Terminal className="h-4 w-4" /><span>最近运行日志</span><span>自动刷新</span></div>
          <pre className="runtime-log">{logsQuery.data?.gateway_log || (logsQuery.isLoading ? '正在读取日志…' : '暂无日志')}</pre>
        </section>
      ) : null}
    </div>
  )
}

function CommandIcon({ status }: { status: string | null }) {
  if (status === 'succeeded') return <CircleCheck className="command-icon command-icon-good" aria-hidden="true" />
  if (status === 'failed' || status === 'rejected') return <CircleX className="command-icon command-icon-bad" aria-hidden="true" />
  if (status === 'pending' || status === 'running') return <Clock3 className="command-icon command-icon-wait" aria-hidden="true" />
  return <Server className="command-icon" aria-hidden="true" />
}
