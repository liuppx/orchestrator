import { Link } from '@tanstack/react-router'
import { ArrowRight, Bot, CandlestickChart, MessageSquareMore, RefreshCw } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Panel } from '../../components/ui/panel'
import { StatCard } from '../../components/ui/stat-card'
import { useAgents } from '../../platform/agents/queries'

const iconByKey = {
  trader: CandlestickChart,
  messenger: MessageSquareMore,
}

const routeByKey = {
  trader: '/agents/trader',
  messenger: '/agents/messenger',
} as const

export function AgentsPage() {
  const { data, isLoading, refetch, isFetching } = useAgents()
  const items = data?.items ?? []
  const availableCount = items.filter((item) => item.available).length
  const categoryCount = new Set(items.map((item) => item.category)).size

  return (
    <div className="space-y-6">
      <div className="rounded-md border border-slate-200 bg-white px-6 py-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-950">应用工作台</h1>
            <p className="mt-1 text-sm text-slate-500">查看已接入业务应用的运行结果与领域配置。</p>
          </div>
          <Button variant="outline" onClick={() => void refetch()}>
            <RefreshCw className={isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
            {isFetching ? '刷新中...' : '刷新'}
          </Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <StatCard icon={Bot} label="业务应用" value={String(items.length)} hint="已注册工作台" />
        <StatCard icon={CandlestickChart} label="目录可用" value={String(availableCount)} hint="应用目录存在" />
        <StatCard
          icon={MessageSquareMore}
          label="应用类别"
          value={String(categoryCount)}
          hint="业务领域分类"
        />
      </div>

      <Panel title="业务工作台" description="业务应用的领域页面与运行控制分开组织。">
        <div className="grid gap-4 xl:grid-cols-2">
          {items.map((agent) => {
            const Icon = iconByKey[agent.key as keyof typeof iconByKey] || Bot
            const to = routeByKey[agent.key as keyof typeof routeByKey]
            return (
              <Link
                key={agent.key}
                to={to ?? '/agents'}
                className="rounded-md border border-slate-200 bg-white p-5 transition hover:border-emerald-200 hover:bg-emerald-50/30"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex items-start gap-4">
                    <div className="flex h-12 w-12 items-center justify-center rounded-md bg-slate-100 text-slate-700">
                      <Icon className="h-5 w-5" />
                    </div>
                    <div>
                      <div className="text-base font-semibold text-slate-950">{agent.display_name}</div>
                      <div className="mt-1 text-sm text-slate-500">{agent.key}</div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Badge variant={agent.available ? 'success' : 'muted'}>
                          {agent.available ? '可用' : '不可用'}
                        </Badge>
                        <Badge variant="muted">分类：{agent.category}</Badge>
                      </div>
                    </div>
                  </div>
                  <ArrowRight className="mt-1 h-4 w-4 text-slate-400" />
                </div>
                <div className="mt-4 text-sm text-slate-500">
                  {to ? '进入工作台查看状态、配置和运行记录。' : '该智能体尚未接入统一 React 工作台。'}
                </div>
              </Link>
            )
          })}
          {isLoading && (
            <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-5 text-sm text-slate-500">
              正在读取智能体列表...
            </div>
          )}
        </div>
      </Panel>
    </div>
  )
}
