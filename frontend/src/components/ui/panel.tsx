import { Separator } from './separator'
import { cn } from '../../platform/core/utils'

export function Panel({
  title,
  description,
  children,
  className,
  bodyClassName,
}: {
  title: string
  description?: string
  children: React.ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={cn('rounded-xl border border-slate-200 bg-white', className)}>
      <div className="px-5 py-4">
        <div className="text-sm font-semibold text-slate-900">{title}</div>
        {description ? <div className="mt-1 text-sm text-slate-500">{description}</div> : null}
      </div>
      <Separator />
      <div className={cn('p-5', bodyClassName)}>{children}</div>
    </section>
  )
}
