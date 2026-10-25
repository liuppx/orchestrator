import { cn } from '../../platform/core/utils'

type BadgeVariant = 'default' | 'success' | 'muted'

const variants: Record<BadgeVariant, string> = {
  default: 'bg-sky-50 text-sky-700',
  success: 'bg-emerald-50 text-emerald-700',
  muted: 'bg-slate-100 text-slate-600',
}

export function Badge({
  children,
  className,
  variant = 'default',
}: {
  children: React.ReactNode
  className?: string
  variant?: BadgeVariant
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium',
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  )
}
