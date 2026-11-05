import { useMemo, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { ArrowUpRight, CalendarClock, CircleAlert, CircleCheck, CircleX, Clock3, Play, PlayCircle, RefreshCw, Search, Server, Square, Terminal } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { useRuntimeHubs, useRuntimeInstanceDiagnose, useRuntimeInstanceLogs, useRuntimeInstances, useScheduledRuns } from '../../platform/agents/queries'
import { useRuntimeInstanceAction } from '../../platform/agents/mutations'
import { scheduleLabel } from '../../platform/core/schedule'
import type { RunState, RuntimeInstance, ScheduledRun } from '../../platform/core/types'
import { CreateTaskPanel } from './create-task-panel'

type StatusFilter = 'all' | 'running' | 'stopped' | 'attention'

const EMPTY_INSTANCES: RuntimeInstance[] = []
const EMPTY_RUNS: ScheduledRun[] = []

function runtimeLabel(value: string) {
  return ({ openclaw: 'OpenClaw', codex: 'Codex CLI', 'claude-code': 'Claude Code CLI' } as Record<string, string>)[value] ?? value
}

function workloadLabel(value: string) {
  return value === 'cli_task' ? 'CLI 任务' : value
}

function stateLabel(value: string) {
  const labels: Record<string, string> = {
    created: '已创建', starting: '启动中', running: '运行中', stopping: '停止中', stopped: '已停止', completed: '已完成', error: '异常', unknown: '未知',
  }
  return labels[value] ?? value
}

function runStateLabel(value: RunState | null | undefined) {
  const labels: Record<RunState, string> = {
    queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败', cancelled: '已取消', unknown: '结果未知',
  }
  return value ? labels[value] : '尚未运行'
}

function runStateVariant(value: RunState | null | undefined): 'success' | 'danger' | 'muted' {
  if (value === 'succeeded') return 'success'
  if (value === 'failed' || value === 'unknown') return 'danger'
  return 'muted'
}

function runTriggerLabel(value: ScheduledRun['trigger']) {
  return value === 'manual' ? '手动' : '计划'
}

function runDuration(run: ScheduledRun) {
  if (!run.started_at || !run.finished_at) return '—'
  const seconds = Math.max(0, Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.round(seconds / 60)} 分钟`
}

function scheduleEnabled(instance: RuntimeInstance) {
  return instance.desired_state === 'running'
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
  // Gateway logs and diagnosis exist only for OpenClaw; CLI tasks expose per-run logs instead.
  const openclawSelectedId = selected?.runtime_type === 'openclaw' ? selected.id : null
  const logsQuery = useRuntimeInstanceLogs(openclawSelectedId)
  const diagnoseQuery = useRuntimeInstanceDiagnose(openclawSelectedId)
  const runsQuery = useScheduledRuns(selected?.deployment_mode === 'scheduled' ? selected.id : null)
  const runs = runsQuery.data?.items ?? EMPTY_RUNS
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

      <CreateTaskPanel />

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
                  <td>{runtimeLabel(instance.runtime_type)}</td>
                  <td><div className="state-pair"><span>{workloadLabel(instance.workload_type)}</span>{instance.deployment_mode === 'scheduled' ? <Badge variant="muted"><CalendarClock className="mr-1 h-3 w-3" aria-hidden="true" />定时任务</Badge> : null}</div></td>
                  <td>
                    {instance.deployment_mode === 'scheduled' ? (
                      <div className="state-pair">
                        <span>{scheduleLabel(instance.schedule)} · {scheduleEnabled(instance) ? '已启用' : '未启用'}</span>
                        <Badge variant={instance.schedule_executed ? runStateVariant(instance.latest_run_state) : 'muted'}>
                          {instance.schedule_executed ? `上次${runStateLabel(instance.latest_run_state)}` : '尚未运行'}
                        </Badge>
                      </div>
                    ) : (
                      <div className="state-pair"><span>{stateLabel(instance.desired_state)}</span><Badge variant={instance.observed_state === 'running' ? 'success' : ['error', 'unknown'].includes(instance.observed_state) ? 'danger' : 'muted'}>{stateLabel(instance.observed_state)}</Badge></div>
                    )}
                  </td>
                  <td className="model-cell">{instance.model || '—'}</td>
                  <td>{instance.endpoint ? <code>{instance.endpoint.replace('http://', '')}</code> : '—'}</td>
                  <td>
                    {instance.deployment_mode === 'scheduled' ? (
                      <div className="flex flex-wrap gap-2">
                        {scheduleEnabled(instance) ? (
                          <Button variant="outline" size="sm" aria-label={`停用 ${instance.name} 的计划`} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'stop' })}><Square className="h-3.5 w-3.5" />停用</Button>
                        ) : (
                          <Button variant="outline" size="sm" aria-label={`启用 ${instance.name} 的计划`} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'start' })}><Play className="h-3.5 w-3.5" />启用</Button>
                        )}
                        <Button variant="outline" size="sm" aria-label={`立即运行 ${instance.name} 一次`} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'run' })}><PlayCircle className="h-3.5 w-3.5" />运行一次</Button>
                      </div>
                    ) : instance.observed_state === 'running' ? (
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
                <Badge variant={instance.deployment_mode === 'scheduled' ? (instance.schedule_executed ? runStateVariant(instance.latest_run_state) : 'muted') : instance.observed_state === 'running' ? 'success' : ['error', 'unknown'].includes(instance.observed_state) ? 'danger' : 'muted'}>
                  {instance.deployment_mode === 'scheduled' ? (instance.schedule_executed ? `上次${runStateLabel(instance.latest_run_state)}` : '尚未运行') : stateLabel(instance.observed_state)}
                </Badge>
              </div>
              <div className="runtime-mobile-meta">
                <div><span>机器 Hub</span><strong>{instance.hub_id ?? '未分配'}</strong></div>
                <div><span>运行时</span><strong>{runtimeLabel(instance.runtime_type)} / {workloadLabel(instance.workload_type)}</strong></div>
                {instance.deployment_mode === 'scheduled' ? <div><span>计划</span><strong>{scheduleLabel(instance.schedule)} · {scheduleEnabled(instance) ? '已启用' : '未启用'}</strong></div> : null}
                <div><span>最近命令</span><strong><CommandIcon status={instance.latest_command_status} />{commandStateLabel(instance.latest_command_status)}</strong></div>
                <div><span>模型</span><strong>{instance.model || '—'}</strong></div>
              </div>
              <div className="runtime-mobile-card-footer">
                {instance.endpoint ? <code>{instance.endpoint.replace('http://', '')}</code> : <span className="text-xs text-slate-500">{runtimeLabel(instance.runtime_type)}</span>}
                {instance.deployment_mode === 'scheduled' ? (
                  <div className="flex flex-wrap gap-2">
                    {scheduleEnabled(instance) ? (
                      <Button variant="outline" size="sm" aria-label={'停用 ' + instance.name + ' 的计划'} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'stop' })}><Square className="h-3.5 w-3.5" />停用</Button>
                    ) : (
                      <Button variant="outline" size="sm" aria-label={'启用 ' + instance.name + ' 的计划'} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'start' })}><Play className="h-3.5 w-3.5" />启用</Button>
                    )}
                    <Button variant="outline" size="sm" aria-label={'立即运行 ' + instance.name + ' 一次'} disabled={runtimeAction.isPending} onClick={() => runtimeAction.mutate({ instanceId: instance.id, action: 'run' })}><PlayCircle className="h-3.5 w-3.5" />运行一次</Button>
                  </div>
                ) : instance.observed_state === 'running' ? (
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
            <div><h2 id="runtime-detail-heading">{selected.name}</h2><p>{selected.id} · {runtimeLabel(selected.runtime_type)}{selected.process_id ? ` · PID ${selected.process_id}` : ''} · 更新于 {new Date(selected.updated_at).toLocaleString()}</p></div>
            {selected.runtime_type === 'openclaw' ? <Link to="/agents/messenger" className="text-link">打开消息工作台 <ArrowUpRight className="h-4 w-4" /></Link> : null}
          </div>
          {selected.last_error ? <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />{selected.last_error}</div> : null}
          <div className="detail-grid">
            <div><span>部署形态</span><strong>{selected.deployment_mode === 'scheduled' ? '定时任务' : '常驻服务'}</strong></div>
            {selected.deployment_mode === 'scheduled' ? <div><span>调度计划</span><strong>{scheduleLabel(selected.schedule)}</strong></div> : null}
            <div><span>期望状态</span><strong>{selected.deployment_mode === 'scheduled' ? (scheduleEnabled(selected) ? '计划已启用' : '计划未启用') : stateLabel(selected.desired_state)}</strong></div>
            <div><span>观测状态</span><strong>{selected.deployment_mode === 'scheduled' ? (selected.schedule_executed ? `上次运行${runStateLabel(selected.latest_run_state)}` : '尚未运行') : stateLabel(selected.observed_state)}</strong></div>
            <div><span>机器 Hub</span><strong>{selected.hub_id ?? '未分配'} · {hubStateLabel(selected.hub_status)}</strong></div>
            <div><span>最近命令</span><strong>{commandStateLabel(selected.latest_command_status)}</strong></div>
            {selected.endpoint ? <div><span>本机地址</span><code>{selected.endpoint}</code></div> : null}
            {selected.runtime_type === 'openclaw' ? <div><span>诊断建议</span><strong>{diagnoseQuery.data?.recommended_action || (diagnoseQuery.isLoading ? '检查中…' : '暂无')}</strong></div> : null}
            {selected.task ? <div><span>执行策略</span><strong>{selected.task.policy ?? '默认'} · 超时 {Math.round(selected.task.run_timeout_seconds / 60)} 分钟</strong></div> : null}
            {selected.task ? <div><span>模型凭证</span><strong>{selected.task.model_credential_configured ? '已配置（存于实例本机）' : '使用本机 CLI 登录'}</strong></div> : null}
          </div>
          {selected.task ? (
            <>
              <div className="log-heading"><Terminal className="h-4 w-4" /><span>任务提示词</span><span>每次运行执行同一任务</span></div>
              <pre className="runtime-log">{selected.task.prompt}</pre>
            </>
          ) : null}
          {selected.deployment_mode === 'scheduled' ? (
            <>
              <div className="log-heading">
                <CalendarClock className="h-4 w-4" />
                <span>运行历史</span>
                <span>{runsQuery.isFetching ? '更新中…' : `${runsQuery.data?.total ?? 0} 条 · 自动刷新`}</span>
              </div>
              {runsQuery.error ? <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" />{runsQuery.error.message}</div> : null}
              {!runsQuery.isLoading && !runsQuery.error && runs.length === 0 ? (
                <div className="empty-inline" role="status">
                  尚无运行记录。启用计划后机器 Hub 会按计划在本机触发；也可以点"运行一次"立即执行。当前 OpenClaw 的单次运行是一次诊断探针，用于验证调度链路，不是 Agent 任务。
                </div>
              ) : null}
              {runs.length > 0 ? (
                <div className="table-scroll">
                  <table className="runtime-table" aria-label="运行历史">
                    <thead><tr><th>触发时间</th><th>来源</th><th>结果</th><th>退出码</th><th>耗时</th><th>日志</th></tr></thead>
                    <tbody>
                      {runs.map((run) => (
                        <tr key={run.id}>
                          <td>{new Date(run.scheduled_for).toLocaleString()}</td>
                          <td>{runTriggerLabel(run.trigger)}</td>
                          <td><Badge variant={runStateVariant(run.state)}>{runStateLabel(run.state)}</Badge></td>
                          <td>{run.exit_code ?? '—'}</td>
                          <td>{runDuration(run)}</td>
                          <td>{run.log_ref ? <code>{run.log_ref}</code> : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </>
          ) : null}
          {selected.runtime_type === 'openclaw' ? (
            <>
              <div className="log-heading"><Terminal className="h-4 w-4" /><span>最近运行日志</span><span>自动刷新</span></div>
              <pre className="runtime-log">{logsQuery.data?.gateway_log || (logsQuery.isLoading ? '正在读取日志…' : '暂无日志')}</pre>
            </>
          ) : null}
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
