import { Link } from '@tanstack/react-router'
import { useEffect, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import {
  AlertTriangle,
  ChevronRight,
  QrCode,
  MessageSquareMore,
  Play,
  RadioTower,
  RefreshCw,
  Rows3,
  SearchCheck,
  SquareTerminal,
  Wrench,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { Label } from '../../components/ui/label'
import { Panel } from '../../components/ui/panel'
import { ScrollArea } from '../../components/ui/scroll-area'
import { Select } from '../../components/ui/select'
import { StatCard } from '../../components/ui/stat-card'
import {
  useMessengerInstanceAction,
  useMessengerInstanceCreate,
  useMessengerWhatsappPair,
} from '../../platform/agents/mutations'
import {
  useMessengerInstanceDiagnose,
  useMessengerInstanceLogs,
  useMessengerInstances,
  useMessengerAgentTypes,
  useAgentWorkspaceSummary,
} from '../../platform/agents/queries'
import { cn } from '../../platform/core/utils'

type MetricCard = {
  label: string
  value: string
  icon: LucideIcon
}

type MessengerCreateForm = {
  kind: string
  name: string
  model: string
  template: string
  dingtalk_client_id: string
  dingtalk_client_secret: string
}

export function MessengerPage() {
  const { data, isLoading, isFetching, refetch } = useAgentWorkspaceSummary('messenger')
  const instancesQuery = useMessengerInstances()
  const agentTypesQuery = useMessengerAgentTypes()
  const summaryState = data?.state || {}
  const instanceCount =
    typeof summaryState.instanceCount === 'number' ? summaryState.instanceCount : 0
  const runningCount =
    typeof summaryState.runningCount === 'number' ? summaryState.runningCount : 0
  const instances = instancesQuery.data?.items ?? []
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null)
  const selectedInstance = useMemo(
    () => instances.find((item) => item.id === selectedInstanceId) ?? instances[0] ?? null,
    [instances, selectedInstanceId],
  )
  const logsQuery = useMessengerInstanceLogs(selectedInstance?.id ?? null)
  const diagnoseQuery = useMessengerInstanceDiagnose(selectedInstance?.id ?? null)
  const startAction = useMessengerInstanceAction(selectedInstance?.id ?? null, 'start')
  const stopAction = useMessengerInstanceAction(selectedInstance?.id ?? null, 'stop')
  const createInstance = useMessengerInstanceCreate()
  const pairWhatsapp = useMessengerWhatsappPair(selectedInstance?.id ?? null)
  const form = useForm<MessengerCreateForm>({
    defaultValues: {
      kind: 'whatsapp',
      name: '',
      model: instancesQuery.data?.defaultModel ?? 'gpt-5.3-codex',
      template: 'ecommerce-toy',
      dingtalk_client_id: '',
      dingtalk_client_secret: '',
    },
  })

  useEffect(() => {
    if (!selectedInstanceId && instances[0]?.id) {
      setSelectedInstanceId(instances[0].id)
    }
  }, [instances, selectedInstanceId])

  useEffect(() => {
    if (instancesQuery.data?.defaultModel) {
      form.setValue('model', instancesQuery.data.defaultModel)
    }
  }, [form, instancesQuery.data?.defaultModel])

  const cards: MetricCard[] = [
    { label: '可用性', value: data?.available ? '可用' : '缺失', icon: MessageSquareMore },
    { label: '实例数量', value: String(instanceCount), icon: Rows3 },
    { label: '运行中', value: String(runningCount), icon: RadioTower },
  ]
  const recentTemplate =
    typeof summaryState.template === 'string' && summaryState.template ? summaryState.template : '未设置'
  const kinds = Array.isArray(summaryState.kinds) ? summaryState.kinds.join(' / ') : 'WhatsApp / DingTalk'
  const selectedKind = form.watch('kind')
  const agentTypes = agentTypesQuery.data?.agentTypes ?? []

  async function onCreate(values: MessengerCreateForm) {
    await createInstance.mutateAsync({
      kind: values.kind,
      name: values.name,
      model: values.model || null,
      template: values.template || null,
      dingtalk_client_id: values.dingtalk_client_id || null,
      dingtalk_client_secret: values.dingtalk_client_secret || null,
    })
    form.reset({
      ...values,
      name: '',
    })
  }

  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-slate-200 bg-white px-6 py-5">
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <Link to="/agents" className="hover:text-slate-900">
            智能体
          </Link>
          <ChevronRight className="h-4 w-4" />
          <span className="text-slate-900">信使</span>
        </div>
        <div className="mt-3 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-950">信使工作台</h1>
            <p className="mt-1 text-sm text-slate-500">统一承载多实例消息智能体，当前先展示摘要与迁移边界。</p>
          </div>
          <div className="flex items-center gap-3">
            <Badge variant={data?.available ? 'success' : 'muted'}>{data?.available ? '已接入' : '未接入'}</Badge>
            <Button variant="outline" onClick={() => void refetch()}>
              <RefreshCw className={isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
              {isFetching ? '刷新中...' : '刷新'}
            </Button>
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {cards.map((card) => {
          return <StatCard key={card.label} icon={card.icon} label={card.label} value={card.value} />
        })}
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <Panel title="运行摘要" description="先聚焦多实例编排的总览，后续再迁入实例列表、日志和配对。">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-lg bg-slate-50 p-4">
              <div className="text-xs text-slate-500">支持通道</div>
              <div className="mt-2 text-sm font-semibold text-slate-950">{kinds}</div>
            </div>
            <div className="rounded-lg bg-slate-50 p-4">
              <div className="text-xs text-slate-500">默认模板</div>
              <div className="mt-2 text-sm font-semibold text-slate-950">{recentTemplate}</div>
            </div>
            <div className="rounded-lg bg-slate-50 p-4">
              <div className="text-xs text-slate-500">实例数量</div>
              <div className="mt-2 text-sm font-semibold text-slate-950">{instanceCount}</div>
            </div>
            <div className="rounded-lg bg-slate-50 p-4">
              <div className="text-xs text-slate-500">运行中</div>
              <div className="mt-2 text-sm font-semibold text-slate-950">{runningCount}</div>
            </div>
          </div>
          {isLoading ? <div className="mt-4 text-sm text-slate-500">正在读取信使摘要...</div> : null}
        </Panel>

        <Panel title="迁移边界" description="当前 React Hub 只承接信使的总览，实例级操作仍由旧控制台处理。">
          <div className="space-y-3">
            {[
              '当前 React 前端只接入 summary。',
              '实例创建、日志、配对、诊断仍由现有静态控制台承载。',
              '下一阶段优先迁入实例列表、单实例日志与诊断。',
            ].map((item) => (
              <div key={item} className="flex items-start gap-3 rounded-lg bg-slate-50 p-4">
                <Wrench className="mt-0.5 h-4 w-4 text-slate-400" />
                <div className="text-sm text-slate-700">{item}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel title="创建实例" description="直接在 React Hub 内创建 WhatsApp / DingTalk 实例，先覆盖最常见入口。">
        <form onSubmit={form.handleSubmit(onCreate)} className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="grid gap-2">
              <Label>类型</Label>
              <Select {...form.register('kind')}>
                {agentTypes.length ? (
                  agentTypes.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))
                ) : (
                  <>
                    <option value="whatsapp">WhatsApp</option>
                    <option value="dingtalk">DingTalk</option>
                  </>
                )}
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>名称</Label>
              <Input {...form.register('name', { required: true })} placeholder="例如：whatsapp-shop-01" />
            </div>
            <div className="grid gap-2">
              <Label>模型</Label>
              <Input {...form.register('model')} />
            </div>
            <div className="grid gap-2">
              <Label>模板</Label>
              <Select {...form.register('template')}>
                <option value="ecommerce-toy">ecommerce-toy</option>
                <option value="generic">generic</option>
              </Select>
            </div>
            {selectedKind === 'dingtalk' ? (
              <>
                <div className="grid gap-2">
                  <Label>钉钉 Client ID</Label>
                  <Input {...form.register('dingtalk_client_id', { required: selectedKind === 'dingtalk' })} />
                </div>
                <div className="grid gap-2 md:col-span-2 xl:col-span-3">
                  <Label>钉钉 Client Secret</Label>
                  <Input
                    {...form.register('dingtalk_client_secret', { required: selectedKind === 'dingtalk' })}
                    type="password"
                  />
                </div>
              </>
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={createInstance.isPending}>
              <Play className="h-4 w-4" />
              {createInstance.isPending ? '创建中...' : '创建实例'}
            </Button>
            {createInstance.data ? (
              <span className="text-sm text-emerald-700">实例 {createInstance.data.id} 已创建</span>
            ) : null}
            {createInstance.error ? <span className="text-sm text-rose-700">{createInstance.error.message}</span> : null}
          </div>
        </form>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <Panel
          title="实例列表"
          description="先迁入实例清单与基础启停动作，后续再补创建、配对和诊断。"
          bodyClassName="p-0"
        >
          <div className="flex items-center justify-between px-5 py-4">
            <div className="text-sm text-slate-500">共 {instances.length} 个实例</div>
            <Button variant="outline" size="sm" onClick={() => void instancesQuery.refetch()}>
              <RefreshCw className={instancesQuery.isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
              刷新
            </Button>
          </div>
          <ScrollArea className="h-[620px]">
            <div className="space-y-3 px-5 pb-5">
              {instances.length ? (
                instances.map((instance) => (
                  <button
                    key={instance.id}
                    type="button"
                    onClick={() => setSelectedInstanceId(instance.id)}
                    className={cn(
                      'block w-full rounded-lg border p-4 text-left transition',
                      selectedInstance?.id === instance.id
                        ? 'border-sky-300 bg-sky-50'
                        : 'border-slate-200 bg-white hover:border-slate-300',
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-sm font-semibold text-slate-950">{instance.name || instance.id}</div>
                        <div className="mt-1 text-xs text-slate-500">{instance.kind}</div>
                      </div>
                      <Badge variant={instance.status === 'running' ? 'success' : 'muted'}>
                        {instance.status}
                      </Badge>
                    </div>
                    <div className="mt-3 grid gap-2 text-xs text-slate-500">
                      <div>端口：{instance.port}</div>
                      <div>模型：{instance.model}</div>
                      <div>Profile：{instance.profile}</div>
                    </div>
                  </button>
                ))
              ) : (
                <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
                  暂无实例。实例创建还在旧控制台。
                </div>
              )}
            </div>
          </ScrollArea>
        </Panel>

        <Panel
          title="实例详情"
          description="查看选中实例的运行状态、日志与基础动作。"
          bodyClassName="space-y-4"
        >
          {selectedInstance ? (
            <>
              <div className="flex flex-wrap items-start justify-between gap-4 rounded-lg bg-slate-50 p-4">
                <div>
                  <div className="text-base font-semibold text-slate-950">{selectedInstance.name || selectedInstance.id}</div>
                  <div className="mt-1 text-sm text-slate-500">
                    {selectedInstance.kind} · {selectedInstance.model}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Badge variant={selectedInstance.status === 'running' ? 'success' : 'muted'}>
                      {selectedInstance.status}
                    </Badge>
                    <Badge variant="muted">端口：{selectedInstance.port}</Badge>
                    <Badge variant="muted">PID：{selectedInstance.pid ?? '-'}</Badge>
                  </div>
                </div>
                <div className="flex flex-wrap gap-3">
                  <Button
                    onClick={() => startAction.mutate()}
                    disabled={startAction.isPending || selectedInstance.status === 'running'}
                  >
                    <Play className="h-4 w-4" />
                    {startAction.isPending ? '启动中...' : '启动'}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => stopAction.mutate()}
                    disabled={stopAction.isPending || selectedInstance.status !== 'running'}
                  >
                    <SquareTerminal className="h-4 w-4" />
                    {stopAction.isPending ? '停止中...' : '停止'}
                  </Button>
                  {selectedInstance.kind === 'whatsapp' ? (
                    <Button
                      variant="outline"
                      onClick={() => pairWhatsapp.mutate()}
                      disabled={pairWhatsapp.isPending}
                    >
                      <QrCode className="h-4 w-4" />
                      {pairWhatsapp.isPending ? '配对中...' : '配对'}
                    </Button>
                  ) : null}
                  <Button variant="outline" onClick={() => void diagnoseQuery.refetch()} disabled={diagnoseQuery.isFetching}>
                    <SearchCheck className="h-4 w-4" />
                    {diagnoseQuery.isFetching ? '诊断中...' : '诊断'}
                  </Button>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded-lg border border-slate-200 p-4">
                  <div className="text-xs text-slate-500">根目录</div>
                  <div className="mt-2 text-sm text-slate-900 break-all">{selectedInstance.root_dir}</div>
                </div>
                <div className="rounded-lg border border-slate-200 p-4">
                  <div className="text-xs text-slate-500">日志目录</div>
                  <div className="mt-2 text-sm text-slate-900 break-all">{selectedInstance.logs_dir}</div>
                </div>
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <div className="rounded-lg bg-slate-950 p-4">
                  <div className="mb-2 text-xs text-slate-400">gateway.log</div>
                  <ScrollArea className="h-[260px]">
                    <pre className="whitespace-pre-wrap text-xs text-slate-100">
                      {logsQuery.data?.gateway_log || '暂无 gateway 日志'}
                    </pre>
                  </ScrollArea>
                </div>
                <div className="rounded-lg bg-slate-950 p-4">
                  <div className="mb-2 text-xs text-slate-400">events.log / pair.log</div>
                  <ScrollArea className="h-[260px]">
                    <pre className="whitespace-pre-wrap text-xs text-slate-100">
                      {[logsQuery.data?.events_log, logsQuery.data?.pair_log].filter(Boolean).join('\n\n')}
                    </pre>
                  </ScrollArea>
                </div>
              </div>

              {selectedInstance.kind === 'whatsapp' && logsQuery.data?.pair_qr_ascii ? (
                <div className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-slate-900">WhatsApp 配对二维码</div>
                      <div className="mt-1 text-xs text-slate-500">{logsQuery.data.pair_hint || '请在手机中扫码。'}</div>
                    </div>
                    <Badge variant="muted">{logsQuery.data.pair_status}</Badge>
                  </div>
                  <pre className="mt-4 overflow-x-auto rounded-lg bg-slate-950 p-4 text-[10px] leading-tight text-slate-100">
                    {logsQuery.data.pair_qr_ascii}
                  </pre>
                </div>
              ) : null}

              <div className="rounded-lg border border-slate-200 bg-white p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-900">
                  <AlertTriangle className="h-4 w-4" />
                  诊断结果
                </div>
                {diagnoseQuery.data ? (
                  <div className="space-y-3">
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                      <div className="rounded-lg bg-slate-50 p-3">
                        <div className="text-xs text-slate-500">Gateway</div>
                        <div className="mt-1 text-sm font-medium text-slate-900">
                          {diagnoseQuery.data.gateway_reachable ? 'reachable' : 'unreachable'}
                        </div>
                      </div>
                      <div className="rounded-lg bg-slate-50 p-3">
                        <div className="text-xs text-slate-500">Pair Status</div>
                        <div className="mt-1 text-sm font-medium text-slate-900">{diagnoseQuery.data.pair_status}</div>
                      </div>
                      <div className="rounded-lg bg-slate-50 p-3">
                        <div className="text-xs text-slate-500">Transport</div>
                        <div className="mt-1 text-sm font-medium text-slate-900">
                          {diagnoseQuery.data.transport_established ? 'established' : 'not established'}
                        </div>
                      </div>
                      <div className="rounded-lg bg-slate-50 p-3">
                        <div className="text-xs text-slate-500">Recommended</div>
                        <div className="mt-1 text-sm font-medium text-slate-900">
                          {diagnoseQuery.data.recommended_action || '-'}
                        </div>
                      </div>
                    </div>
                    {diagnoseQuery.data.pair_hint ? (
                      <div className="rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                        {diagnoseQuery.data.pair_hint}
                      </div>
                    ) : null}
                    <div className="rounded-lg bg-slate-950 p-4">
                      <div className="mb-2 text-xs text-slate-400">诊断证据</div>
                      <pre className="whitespace-pre-wrap text-xs text-slate-100">
                        {diagnoseQuery.data.evidence.join('\n')}
                      </pre>
                    </div>
                  </div>
                ) : (
                  <div className="text-sm text-slate-500">点击“诊断”后，在这里查看推荐动作和证据链。</div>
                )}
              </div>

              {(selectedInstance.last_error ||
                startAction.error ||
                stopAction.error ||
                logsQuery.error ||
                pairWhatsapp.error ||
                diagnoseQuery.error) && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
                  {selectedInstance.last_error ||
                    startAction.error?.message ||
                    stopAction.error?.message ||
                    logsQuery.error?.message ||
                    pairWhatsapp.error?.message ||
                    diagnoseQuery.error?.message}
                </div>
              )}
            </>
          ) : (
            <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-4 text-sm text-slate-500">
              选择一个实例后，在这里查看状态与日志。
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
