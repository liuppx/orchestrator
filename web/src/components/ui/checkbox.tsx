import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import { Check } from 'lucide-react'

import { cn } from '../../platform/core/utils'

export function Checkbox({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        'peer flex h-4 w-4 shrink-0 items-center justify-center rounded border border-slate-300 bg-white text-emerald-700 outline-none focus:ring-2 focus:ring-emerald-100 data-[state=checked]:border-emerald-700 data-[state=checked]:bg-emerald-700 data-[state=checked]:text-white',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator>
        <Check className="h-3 w-3" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}
