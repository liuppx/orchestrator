import * as React from 'react'

import { cn } from '../../platform/core/utils'

type ButtonVariant = 'default' | 'outline' | 'ghost' | 'danger'
type ButtonSize = 'default' | 'sm'

const variants: Record<ButtonVariant, string> = {
  default: 'bg-sky-600 text-white hover:bg-sky-700',
  outline: 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50',
  ghost: 'bg-transparent text-slate-600 hover:bg-slate-100 hover:text-slate-900',
  danger: 'border border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100',
}

const sizes: Record<ButtonSize, string> = {
  default: 'h-10 px-4 text-sm',
  sm: 'h-9 px-3 text-sm',
}

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'default', size = 'default', ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition disabled:pointer-events-none disabled:opacity-60',
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  )
})
