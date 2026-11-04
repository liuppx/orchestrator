import type { InstanceSchedule } from './types'

/** Human-readable schedule summary for list rows and detail panels. */
export function scheduleLabel(schedule: InstanceSchedule | null | undefined): string {
  if (!schedule) return '—'
  if (schedule.kind === 'interval') {
    const seconds = schedule.seconds ?? 0
    if (seconds % 3600 === 0) return `每 ${seconds / 3600} 小时`
    if (seconds % 60 === 0) return `每 ${seconds / 60} 分钟`
    return `每 ${seconds} 秒`
  }
  const tz = schedule.timezone ? ` (${schedule.timezone})` : ''
  return `cron ${schedule.expr ?? ''}${tz}`
}
