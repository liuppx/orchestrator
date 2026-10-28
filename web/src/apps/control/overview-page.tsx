import { Link } from '@tanstack/react-router'
import { ArrowUpRight, CircleAlert, CircleCheck, Cpu, RefreshCw, Server } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { useRuntimeInstances, useRuntimeProviders } from '../../platform/agents/queries'

export function OverviewPage() {
  const instancesQuery = useRuntimeInstances()
  const providersQuery = useRuntimeProviders()
  const instances = instancesQuery.data?.items ?? []
  const providers = providersQuery.data?.items ?? []

  return (
    <div className="control-page">
      <header className="page-heading">
        <div>
          <h1>运行概览</h1>
          <p>查看当前节点上的 Agent Runtime 和受管实例。</p>
        </div>
        <Button variant="outline" onClick={() => void Promise.all([instancesQuery.refetch(), providersQuery.refetch()])}>
          <RefreshCw className={instancesQuery.isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
          刷新
        </Button>
      </header>

      <section className="metric-strip" aria-label="运行统计">
        <div className="metric-cell">
          <span>受管实例</span>
          <strong>{instancesQuery.data?.total ?? '—'}</strong>
        </div>
        <div className="metric-cell metric-good">
          <span>运行中</span>
          <strong>{instancesQuery.data?.running ?? '—'}</strong>
        </div>
        <div className="metric-cell">
          <span>已停止</span>
          <strong>{instancesQuery.data?.stopped ?? '—'}</strong>
        </div>
        <div className="metric-cell metric-attention">
          <span>需要关注</span>
          <strong>{instancesQuery.data?.attention ?? '—'}</strong>
        </div>
      </section>

      {instancesQuery.error ? (
        <div className="inline-error" role="alert">
          <CircleAlert className="h-4 w-4" />
          <span>{instancesQuery.error.message}</span>
          <Button variant="outline" size="sm" onClick={() => void instancesQuery.refetch()}>重试</Button>
        </div>
      ) : null}

      <section className="surface-section">
        <div className="section-heading">
          <div>
            <h2>Runtime Providers</h2>
            <p>只显示当前控制节点已实现并可检测的 Runtime Provider。</p>
          </div>
          <Link to="/runtime" className="text-link">查看实例 <ArrowUpRight className="h-4 w-4" /></Link>
        </div>
        {providersQuery.isLoading ? <div className="empty-inline">正在读取 Runtime Provider 状态…</div> : null}
        {providersQuery.error ? (
          <div className="inline-error" role="alert">
            <CircleAlert className="h-4 w-4" />
            <span>{providersQuery.error.message}</span>
            <Button variant="outline" size="sm" onClick={() => void providersQuery.refetch()}>重试</Button>
          </div>
        ) : null}
        {providers.map((provider) => (
          <div className="provider-row" key={provider.id}>
            <div className="provider-mark"><Cpu className="h-5 w-5" /></div>
            <div className="provider-copy">
              <strong>{provider.name}</strong>
              <span>{provider.management_mode === 'local-process' ? '本机进程' : provider.management_mode} · {provider.capabilities.length} 项管理能力</span>
            </div>
            <Badge variant={provider.availability === 'configured' ? 'success' : 'muted'}>
              {provider.availability === 'configured' ? '已配置' : '未配置'}
            </Badge>
          </div>
        ))}
      </section>

      <section className="surface-section">
        <div className="section-heading">
          <div>
            <h2>最近实例</h2>
            <p>实例状态以运行节点的最新观测为准。</p>
          </div>
          <Link to="/runtime" className="text-link">全部实例 <ArrowUpRight className="h-4 w-4" /></Link>
        </div>
        {instancesQuery.isLoading ? <div className="empty-inline">正在读取实例…</div> : null}
        {!instancesQuery.isLoading && !instancesQuery.error && instances.length === 0 ? (
          <div className="empty-state">
            <Server className="h-6 w-6" />
            <div><strong>当前没有受管实例</strong><span>创建一个 OpenClaw 实例，开始管理运行状态。</span></div>
            <Link to="/agents/messenger" className="text-link">创建实例 <ArrowUpRight className="h-4 w-4" /></Link>
          </div>
        ) : null}
        {instances.slice(0, 5).map((instance) => (
          <Link className="overview-instance" to="/runtime" key={instance.id}>
            <span className={`state-dot state-${instance.observed_state}`} aria-hidden="true" />
            <strong>{instance.name}</strong>
            <span>{instance.runtime_type} / {instance.workload_type}</span>
            <span className="instance-state">{instance.observed_state}</span>
          </Link>
        ))}
        {!instancesQuery.isLoading && !instancesQuery.error && instances.length > 0 && instancesQuery.data?.attention === 0 ? (
          <div className="healthy-note"><CircleCheck className="h-4 w-4" />没有需要关注的实例</div>
        ) : null}
      </section>

      <footer className="scope-note">
        当前控制面管理本机 OpenClaw 进程。其他 Runtime、远程节点和容器应用尚未接入，不会显示为在线。
      </footer>
    </div>
  )
}
