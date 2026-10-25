import type { PropsWithChildren } from 'react'
import { useEffect } from 'react'
import { Bot, CandlestickChart, MessageSquareMore } from 'lucide-react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'

import { logout } from '../platform/auth/mutations'
import { usePlatformSession } from '../platform/auth/queries'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'

const navItems = [
  { to: '/agents', label: '智能体', icon: Bot },
  { to: '/agents/trader', label: '交易员', icon: CandlestickChart },
  { to: '/agents/messenger', label: '信使', icon: MessageSquareMore },
]

export function AppShell({ children }: PropsWithChildren) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const navigate = useNavigate()
  const { session, isLoading } = usePlatformSession()

  useEffect(() => {
    if (!isLoading && !session.isAuthenticated) {
      void navigate({ to: '/', replace: true })
    }
  }, [isLoading, navigate, session])

  async function handleLogout() {
    await logout()
    await navigate({ to: '/', replace: true })
  }

  if (isLoading || !session.isAuthenticated) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-100 text-sm text-slate-500">
        正在校验会话...
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-slate-100 text-slate-950">
      <div className="mx-auto grid min-h-screen max-w-[1440px] grid-cols-[248px_minmax(0,1fr)]">
        <aside className="border-r border-slate-200 bg-white">
          <div className="flex h-16 items-center border-b border-slate-200 px-5">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-sky-600 text-sm font-semibold text-white">
              HB
            </div>
            <div className="ml-3">
              <div className="text-sm font-semibold">Hub</div>
              <div className="text-xs text-slate-500">Agent Control Plane</div>
            </div>
          </div>
          <nav className="space-y-1 p-3">
            {navItems.map((item) => {
              const Icon = item.icon
              const active = pathname === item.to
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
                    active
                      ? 'bg-sky-50 text-sky-700'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  <Icon className="h-4 w-4" />
                  <span>{item.label}</span>
                </Link>
              )
            })}
          </nav>
        </aside>
        <main className="min-w-0">
          <header className="flex h-16 items-center justify-between border-b border-slate-200 bg-white px-6">
            <div>
              <div className="text-sm font-medium text-slate-900">智能体管理后台</div>
              <div className="text-xs text-slate-500">
                {session.capability.protocol === 'ucan' ? 'Platform session: UCAN ready' : 'Platform session: cookie-wallet'}
              </div>
            </div>
            <div className="flex items-center gap-3">
              <Badge variant="muted">{session.walletShort}</Badge>
              <Badge variant={session.capability.protocol === 'ucan' ? 'success' : 'muted'}>
                {session.capability.protocol === 'ucan' ? 'UCAN' : 'Wallet'}
              </Badge>
              <Button type="button" variant="outline" size="sm" onClick={() => void handleLogout()}>
                退出
              </Button>
            </div>
          </header>
          <div className="p-6">{children}</div>
        </main>
      </div>
    </div>
  )
}
