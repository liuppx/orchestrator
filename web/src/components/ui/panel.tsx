import { useId } from 'react'

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
  const headingId = useId()

  return (
    <section className={cn('product-panel rounded-md border border-slate-200 bg-white', className)} aria-labelledby={headingId}>
      <header className="product-panel-heading px-5 py-4">
        <h2 id={headingId} className="text-sm font-semibold text-slate-900">{title}</h2>
        {description ? <p className="mt-1 text-sm text-slate-500">{description}</p> : null}
      </header>
      <Separator />
      <div className={cn('p-5', bodyClassName)}>{children}</div>
    </section>
  )
}
