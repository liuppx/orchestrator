import type { PropsWithChildren } from 'react'
import { useEffect } from 'react'
import { AppWindow, Boxes, CandlestickChart, LayoutDashboard, LogOut, MessageSquareMore } from 'lucide-react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'

import { logout } from '../platform/auth/mutations'
import { usePlatformSession } from '../platform/auth/queries'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'

const navItems = [
  { to: '/overview', label: '运行概览', icon: LayoutDashboard, group: '控制面' },
  { to: '/runtime', label: '运行时实例', icon: Boxes, group: '控制面' },
  { to: '/agents', label: '应用工作台', icon: AppWindow, group: '应用' },
  { to: '/agents/trader', label: '交易员', icon: CandlestickChart, group: '应用' },
  { to: '/agents/messenger', label: '信使', icon: MessageSquareMore, group: '应用' },
] as const

function isActive(pathname: string, to: string) {
  return pathname === to || (to === '/agents/trader' && pathname.startsWith('/agents/trader/'))
}

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
    return <div className="session-check">正在验证登录状态…</div>
  }

  return (
    <div className="app-frame">
      <aside className="app-sidebar">
        <Link to="/overview" className="brand-lockup" aria-label="Orchestrator 运行概览">
          <div className="brand-mark">OR</div>
          <div>
            <div className="brand-name">Orchestrator</div>
            <div className="brand-caption">Runtime Control Plane</div>
          </div>
        </Link>
        <nav className="primary-nav" aria-label="主导航">
          {(['控制面', '应用'] as const).map((group) => (
            <div className="nav-group" key={group}>
              <div className="nav-group-label">{group}</div>
              {navItems.filter((item) => item.group === group).map((item) => {
                const Icon = item.icon
                const active = isActive(pathname, item.to)
                return (
                  <Link key={item.to} to={item.to} aria-current={active ? 'page' : undefined} className={`nav-link ${active ? 'is-active' : ''}`}>
                    <Icon className="h-4 w-4" />
                    <span>{item.label}</span>
                  </Link>
                )
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-footnote"><span className="node-indicator" />本地执行节点</div>
      </aside>

      <main className="app-main">
        <header className="topbar">
          <div>
            <div className="topbar-title">运行控制台</div>
            <div className="topbar-subtitle">Agent runtimes and workloads</div>
          </div>
          <div className="topbar-account">
            <Badge variant="muted">{session.walletShort}</Badge>
            <Button type="button" variant="outline" size="sm" aria-label="退出登录" title="退出登录" onClick={() => void handleLogout()}>
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </header>
        <nav className="mobile-nav" aria-label="主导航">
          {navItems.map((item) => {
            const Icon = item.icon
            const active = isActive(pathname, item.to)
            return <Link key={item.to} to={item.to} aria-current={active ? 'page' : undefined} className={active ? 'mobile-nav-link is-active' : 'mobile-nav-link'}><Icon className="h-4 w-4" /><span>{item.label}</span></Link>
          })}
        </nav>
        <div className="main-content">{children}</div>
      </main>
    </div>
  )
}
