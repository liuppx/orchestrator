import { Link } from '@tanstack/react-router'
import { ArrowRight, ArrowUpRight, Boxes, CircleAlert, CircleCheck, Cpu, RefreshCw, Server } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { useRuntimeHubs, useRuntimeInstances, useRuntimeProviders } from '../../platform/agents/queries'

function FlowValue({ value, detail }: { value: string; detail: string }) {
  return (
    <div className="flow-value">
      <strong>{value}</strong>
      <span>{detail}</span>
    </div>
  )
}

export function OverviewPage() {
  const instancesQuery = useRuntimeInstances()
  const providersQuery = useRuntimeProviders()
  const hubsQuery = useRuntimeHubs()
  const instances = instancesQuery.data?.items ?? []
  const providers = providersQuery.data?.items ?? []
  const hubs = hubsQuery.data?.items ?? []
  const configuredProviders = providers.filter((provider) => provider.availability === 'configured').length
  const isRefreshing = instancesQuery.isFetching || providersQuery.isFetching || hubsQuery.isFetching

  return (
    <div className="control-page">
      <header className="page-heading">
        <div>
          <h1>运行概览</h1>
          <p>从机器连接到应用实例，掌握当前执行面的状态。</p>
        </div>
        <Button
          variant="outline"
          onClick={() => void Promise.all([instancesQuery.refetch(), providersQuery.refetch(), hubsQuery.refetch()])}
          disabled={isRefreshing}
        >
          <RefreshCw className={isRefreshing ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
          {isRefreshing ? '正在更新' : '刷新状态'}
        </Button>
      </header>

      <section className="execution-flow" aria-label="运行链路状态">
        <div className="flow-step">
          <span className="flow-icon"><Server className="h-4 w-4" /></span>
          <div className="flow-step-copy">
            <span>机器 Hub</span>
            <FlowValue
              value={hubsQuery.isLoading ? '读取中' : hubsQuery.error ? '不可用' : String(hubsQuery.data?.online ?? 0)}
              detail={hubsQuery.isLoading || hubsQuery.error ? '连接状态' : '在线 / ' + (hubsQuery.data?.total ?? 0) + ' 台'}
            />
          </div>
        </div>
        <ArrowRight className="flow-connector" aria-hidden="true" />
        <div className="flow-step">
          <span className="flow-icon"><Cpu className="h-4 w-4" /></span>
          <div className="flow-step-copy">
            <span>Runtime Provider</span>
            <FlowValue
              value={providersQuery.isLoading ? '读取中' : providersQuery.error ? '不可用' : String(configuredProviders)}
              detail={providersQuery.isLoading || providersQuery.error ? '配置状态' : '已配置 / ' + providers.length + ' 个'}
            />
          </div>
        </div>
        <ArrowRight className="flow-connector" aria-hidden="true" />
        <div className="flow-step">
          <span className="flow-icon"><Boxes className="h-4 w-4" /></span>
          <div className="flow-step-copy">
            <span>受管实例</span>
            <FlowValue
              value={instancesQuery.isLoading ? '读取中' : instancesQuery.error ? '不可用' : String(instancesQuery.data?.running ?? 0)}
              detail={instancesQuery.isLoading || instancesQuery.error ? '运行状态' : '运行中 / ' + (instancesQuery.data?.total ?? 0) + ' 个'}
            />
          </div>
        </div>
      </section>

      <section className="metric-strip" aria-label="实例统计">
        <div className="metric-cell">
          <span>受管实例</span>
          <strong>{instancesQuery.error ? '—' : instancesQuery.data?.total ?? '—'}</strong>
        </div>
        <div className="metric-cell metric-good">
          <span>运行中</span>
          <strong>{instancesQuery.error ? '—' : instancesQuery.data?.running ?? '—'}</strong>
        </div>
        <div className="metric-cell">
          <span>已停止</span>
          <strong>{instancesQuery.error ? '—' : instancesQuery.data?.stopped ?? '—'}</strong>
        </div>
        <div className="metric-cell metric-attention">
          <span>需要关注</span>
          <strong>{instancesQuery.error ? '—' : instancesQuery.data?.attention ?? '—'}</strong>
        </div>
      </section>

      {instancesQuery.error ? (
        <div className="inline-error" role="alert">
          <CircleAlert className="h-4 w-4" />
          <span>无法读取实例状态：{instancesQuery.error.message}</span>
          <Button variant="outline" size="sm" onClick={() => void instancesQuery.refetch()}>重试</Button>
        </div>
      ) : null}

      <div className="overview-grid">
        <section className="surface-section">
          <div className="section-heading">
            <div>
              <h2>最近实例</h2>
              <p>状态由运行节点最近一次上报决定。</p>
            </div>
            <Link to="/runtime" className="text-link">全部实例 <ArrowUpRight className="h-4 w-4" /></Link>
          </div>
          {instancesQuery.isLoading ? <div className="empty-inline">正在读取实例状态…</div> : null}
          {!instancesQuery.isLoading && !instancesQuery.error && instances.length === 0 ? (
            <div className="empty-state">
              <Server className="h-6 w-6" aria-hidden="true" />
              <div><strong>还没有受管实例</strong><span>连接机器 Hub 并创建一个 OpenClaw 实例后，运行状态会显示在这里。</span></div>
              <Link to="/agents/messenger" className="text-link">创建实例 <ArrowUpRight className="h-4 w-4" /></Link>
            </div>
          ) : null}
          {instances.slice(0, 6).map((instance) => (
            <Link className="overview-instance" to="/runtime" key={instance.id}>
              <span className={'state-dot state-' + instance.observed_state} aria-hidden="true" />
              <strong>{instance.name}</strong>
              <span>{instance.runtime_type} / {instance.workload_type}</span>
              <Badge variant={instance.observed_state === 'running' ? 'success' : ['error', 'unknown'].includes(instance.observed_state) ? 'danger' : 'muted'}>
                {instance.observed_state === 'running' ? '运行中' : instance.observed_state === 'error' ? '异常' : instance.observed_state === 'unknown' ? '未知' : '已停止'}
              </Badge>
            </Link>
          ))}
          {!instancesQuery.isLoading && !instancesQuery.error && instances.length > 0 && instancesQuery.data?.attention === 0 ? (
            <div className="healthy-note"><CircleCheck className="h-4 w-4" />当前没有需要关注的实例</div>
          ) : null}
        </section>

        <div className="overview-side">
          <section className="surface-section">
            <div className="section-heading">
              <div><h2>机器 Hub</h2><p>管理实例所在的执行节点。</p></div>
              <Link to="/runtime" className="text-link" aria-label="查看机器和运行实例">查看 <ArrowUpRight className="h-4 w-4" /></Link>
            </div>
            {hubsQuery.isLoading ? <div className="empty-inline">正在检查机器连接…</div> : null}
            {hubsQuery.error ? (
              <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" /><span>{hubsQuery.error.message}</span><Button variant="outline" size="sm" onClick={() => void hubsQuery.refetch()}>重试</Button></div>
            ) : null}
            {!hubsQuery.isLoading && !hubsQuery.error && hubs.length === 0 ? (
              <div className="empty-inline">尚无已关联的机器。机器连接后会出现在这里。</div>
            ) : null}
            {hubs.slice(0, 4).map((hub) => (
              <div className="hub-row" key={hub.id}>
                <span className={'hub-state-mark hub-state-' + hub.status} aria-hidden="true" />
                <div className="hub-row-copy"><strong>{hub.id}</strong><span>{hub.adapter_count} 个 Adapter · {hub.instance_count} 个实例</span></div>
                <span className={'hub-state-label hub-state-label-' + hub.status}>{hub.status === 'online' ? '在线' : hub.status === 'offline' ? '离线' : '未知'}</span>
              </div>
            ))}
          </section>

          <section className="surface-section">
            <div className="section-heading">
              <div><h2>运行时支持</h2><p>显示当前控制面可检测的 Provider。</p></div>
            </div>
            {providersQuery.isLoading ? <div className="empty-inline">正在读取运行时支持…</div> : null}
            {providersQuery.error ? (
              <div className="inline-error" role="alert"><CircleAlert className="h-4 w-4" /><span>{providersQuery.error.message}</span><Button variant="outline" size="sm" onClick={() => void providersQuery.refetch()}>重试</Button></div>
            ) : null}
            {!providersQuery.isLoading && !providersQuery.error && providers.length === 0 ? <div className="empty-inline">当前没有可用 Provider。</div> : null}
            {providers.map((provider) => (
              <div className="provider-row" key={provider.id}>
                <div className="provider-mark"><Cpu className="h-4 w-4" aria-hidden="true" /></div>
                <div className="provider-copy">
                  <strong>{provider.name}</strong>
                  <span>{provider.management_mode === 'machine-hub' ? '通过机器 Hub 管理' : provider.management_mode} · {provider.capabilities.length} 项能力</span>
                </div>
                <Badge variant={provider.availability === 'configured' ? 'success' : 'muted'}>
                  {provider.availability === 'configured' ? '已配置' : '未配置'}
                </Badge>
              </div>
            ))}
          </section>
        </div>
      </div>

      <footer className="scope-note">
        当前版本只展示已接入的 OpenClaw Runtime 能力；未连接或未上报的机器不会标记为在线。
      </footer>
    </div>
  )
}
