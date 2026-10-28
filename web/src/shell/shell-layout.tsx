import { Outlet } from '@tanstack/react-router'

import { AppShell } from './app-shell'

export function ShellLayout() {
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  )
}
