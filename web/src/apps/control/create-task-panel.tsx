import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { CalendarClock, CircleAlert } from 'lucide-react'

import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { Select } from '../../components/ui/select'
import { useRuntimeInstanceCreate } from '../../platform/agents/mutations'
import { useRuntimeProviders } from '../../platform/agents/queries'
import type { CliRuntimeType, CliTaskSpec, InstanceSchedule } from '../../platform/core/types'

type TaskForm = {
  runtime_type: CliRuntimeType
  name: string
  prompt: string
  policy: string
  model: string
  schedule_kind: 'cron' | 'interval'
  schedule_expr: string
  schedule_minutes: string
  schedule_timezone: string
  timeout_minutes: string
}

const RUNTIME_LABELS: Record<CliRuntimeType, string> = { codex: 'Codex CLI', 'claude-code': 'Claude Code CLI' }

/** Only the safe execution policies exist here; the CLIs' bypass modes are not representable in the protocol. */
const POLICIES: Record<CliRuntimeType, { value: string; label: string }[]> = {
  codex: [
    { value: 'read-only', label: 'read-only · 只读沙箱（默认）' },
    { value: 'workspace-write', label: 'workspace-write · 可写工作区' },
  ],
  'claude-code': [
    { value: 'plan', label: 'plan · 仅规划，不执行工具（默认）' },
    { value: 'acceptEdits', label: 'acceptEdits · 自动接受工作区内编辑' },
    { value: 'dontAsk', label: 'dontAsk · 需确认的操作一律拒绝' },
  ],
}

const TEXTAREA_CLASS =
  'flex min-h-28 w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 transition placeholder:text-slate-400 focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100'

function buildSchedule(values: TaskForm): InstanceSchedule {
  if (values.schedule_kind === 'interval') {
    return { kind: 'interval', expr: null, seconds: Math.round(Number(values.schedule_minutes) * 60), timezone: null }
  }
  return { kind: 'cron', expr: values.schedule_expr.trim(), seconds: null, timezone: values.schedule_timezone.trim() || null }
}

function buildTask(values: TaskForm): CliTaskSpec {
  const task: CliTaskSpec = { prompt: values.prompt.trim(), run_timeout_seconds: Math.round(Number(values.timeout_minutes) * 60) }
  if (values.runtime_type === 'codex') task.codex_sandbox = values.policy as CliTaskSpec['codex_sandbox']
  else task.claude_permission_mode = values.policy as CliTaskSpec['claude_permission_mode']
  return task
}

export function CreateTaskPanel() {
  const providers = useRuntimeProviders()
  const create = useRuntimeInstanceCreate()
  const available = (providers.data?.items ?? []).filter(
    (item): item is typeof item & { id: CliRuntimeType } => (item.id === 'codex' || item.id === 'claude-code') && item.availability === 'configured',
  )
  const form = useForm<TaskForm>({
    defaultValues: {
      runtime_type: 'codex',
      name: '',
      prompt: '',
      policy: 'read-only',
      model: '',
      schedule_kind: 'cron',
      schedule_expr: '0 9 * * 1-5',
      schedule_minutes: '60',
      schedule_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone ?? '',
      timeout_minutes: '15',
    },
  })
  const runtime = form.watch('runtime_type')
  const scheduleKind = form.watch('schedule_kind')
  const policies = POLICIES[runtime]
  useEffect(() => {
    // Policies are runtime-specific; fall back to that runtime's safe default when switching.
    if (!policies.some((item) => item.value === form.getValues('policy'))) {
      form.setValue('policy', policies[0].value)
    }
  }, [form, policies])

  async function onSubmit(values: TaskForm) {
    await create.mutateAsync({
      runtime_type: values.runtime_type,
      kind: 'cli_task',
      name: values.name.trim(),
      model: values.model.trim() || null,
      deployment_mode: 'scheduled',
      schedule: buildSchedule(values),
      task: buildTask(values),
    })
    form.reset({ ...values, name: '', prompt: '' })
  }

  return (
    <section className="surface-section" aria-labelledby="create-task-heading">
      <div className="section-heading">
        <div>
          <h2 id="create-task-heading">创建定时 CLI 任务</h2>
          <p>在机器 Hub 上按计划运行一次 Codex CLI 或 Claude Code CLI。提示词和执行策略是实例定义，每次运行都执行同一任务；创建后需启用计划才会触发。</p>
        </div>
      </div>
      {providers.isLoading ? <div className="empty-inline">正在读取 Hub 申报的运行时…</div> : null}
      {!providers.isLoading && available.length === 0 ? (
        <div className="empty-inline" role="status">
          当前在线的机器 Hub 没有申报可用的 Codex / Claude Code Adapter。请在 Hub 所在机器安装对应 CLI 并以 <code>--codex</code> / <code>--claude-code</code> 启动 Hub。
        </div>
      ) : null}
      {available.length > 0 ? (
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="grid gap-2">
              <label htmlFor="task-runtime" className="text-sm font-medium text-slate-700">运行时</label>
              <Select id="task-runtime" {...form.register('runtime_type')}>
                {available.map((item) => (
                  <option key={item.id} value={item.id}>{RUNTIME_LABELS[item.id]}</option>
                ))}
              </Select>
            </div>
            <div className="grid gap-2">
              <label htmlFor="task-name" className="text-sm font-medium text-slate-700">名称</label>
              <Input id="task-name" {...form.register('name', { required: true })} placeholder="例如：每日仓库巡检" />
            </div>
            <div className="grid gap-2">
              <label htmlFor="task-policy" className="text-sm font-medium text-slate-700">执行策略</label>
              <Select id="task-policy" {...form.register('policy')}>
                {policies.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </Select>
            </div>
            <div className="grid gap-2">
              <label htmlFor="task-model" className="text-sm font-medium text-slate-700">模型（可选）</label>
              <Input id="task-model" {...form.register('model')} placeholder="留空使用 CLI 默认模型" />
            </div>
            <div className="grid gap-2 md:col-span-2 xl:col-span-4">
              <label htmlFor="task-prompt" className="text-sm font-medium text-slate-700">任务提示词</label>
              <textarea id="task-prompt" className={TEXTAREA_CLASS} {...form.register('prompt', { required: true, maxLength: 8192 })} placeholder="描述这次运行要完成的事。它会原样作为 CLI 的提示词执行，不能包含命令行参数。" />
            </div>
            <div className="grid gap-2">
              <label htmlFor="task-schedule-kind" className="text-sm font-medium text-slate-700">计划类型</label>
              <Select id="task-schedule-kind" {...form.register('schedule_kind')}>
                <option value="cron">Cron 表达式</option>
                <option value="interval">固定间隔</option>
              </Select>
            </div>
            {scheduleKind === 'interval' ? (
              <div className="grid gap-2">
                <label htmlFor="task-schedule-minutes" className="text-sm font-medium text-slate-700">间隔（分钟，≥ 1）</label>
                <Input id="task-schedule-minutes" type="number" min={1} step={1} {...form.register('schedule_minutes', { required: true, min: 1 })} />
              </div>
            ) : (
              <>
                <div className="grid gap-2">
                  <label htmlFor="task-schedule-expr" className="text-sm font-medium text-slate-700">Cron 表达式</label>
                  <Input id="task-schedule-expr" {...form.register('schedule_expr', { required: true })} placeholder="分 时 日 月 周" />
                </div>
                <div className="grid gap-2">
                  <label htmlFor="task-schedule-timezone" className="text-sm font-medium text-slate-700">时区</label>
                  <Input id="task-schedule-timezone" {...form.register('schedule_timezone')} placeholder="例如 Asia/Shanghai" />
                </div>
              </>
            )}
            <div className="grid gap-2">
              <label htmlFor="task-timeout" className="text-sm font-medium text-slate-700">单次超时（分钟）</label>
              <Input id="task-timeout" type="number" min={1} max={1440} step={1} {...form.register('timeout_minutes', { required: true, min: 1, max: 1440 })} />
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={create.isPending}>
              <CalendarClock className="h-4 w-4" />
              {create.isPending ? '创建中…' : '创建任务'}
            </Button>
            {create.data ? <span className="text-sm text-emerald-700">任务 {create.data.name} 已创建（{create.data.id}），计划尚未启用。</span> : null}
            {create.error ? <span className="inline-flex items-center gap-1 text-sm text-rose-700"><CircleAlert className="h-4 w-4" />{create.error.message}</span> : null}
          </div>
        </form>
      ) : null}
    </section>
  )
}
