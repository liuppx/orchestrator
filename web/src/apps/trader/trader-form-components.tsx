import { cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react'

import { Checkbox } from '../../components/ui/checkbox'

import type { TraderConfigForm } from './trader-helpers'

export function ConfigField({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  const labelledControl = isValidElement(children)
    ? cloneElement(children as ReactElement<{ 'aria-label'?: string }>, { 'aria-label': label })
    : children

  return (
    <label className="grid gap-2">
      <span className="text-sm font-medium text-slate-700">{label}</span>
      {labelledControl}
      {hint ? <div className="text-xs text-slate-500">{hint}</div> : null}
    </label>
  )
}

export function StrategyBooleanToggles({
  formWatch,
  formSetValue,
}: {
  formWatch: (name: keyof TraderConfigForm) => boolean
  formSetValue: (name: keyof TraderConfigForm, value: boolean) => void
}) {
  return (
    <div className="flex flex-wrap gap-6 rounded-md bg-slate-50 px-4 py-3">
      {[
        ['enabled', '启用策略'],
        ['dry_run', 'Dry Run'],
        ['enable_buy', '允许买入'],
      ].map(([name, label]) => (
        <label key={name} className="inline-flex items-center gap-3 text-sm text-slate-700">
          <Checkbox
            checked={formWatch(name as keyof TraderConfigForm)}
            onCheckedChange={(checked) => formSetValue(name as keyof TraderConfigForm, Boolean(checked))}
          />
          {label}
        </label>
      ))}
    </div>
  )
}
