import type { PropsWithChildren } from 'react'
import { useEffect, useRef, useState } from 'react'
import { AppWindow, Boxes, CandlestickChart, Check, ChevronDown, Copy, LayoutDashboard, LogOut, MessageSquareMore, UserRound } from 'lucide-react'
import { Link, useNavigate, useRouterState } from '@tanstack/react-router'

import { logout } from '../platform/auth/mutations'
import { usePlatformSession } from '../platform/auth/queries'
import { queryClient } from '../platform/query/query-client'

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

function authMethodLabel(authMethod: string | null, authType: string | null) {
  if (authMethod === 'passport') return '夜莺通行证'
  if (authMethod === 'wallet_identity') return '钱包身份'
  if (authType === 'wallet_identity') return '钱包身份'
  if (authType === 'wallet_plugin') return '兼容钱包'
  return '已验证身份'
}

function formatSessionExpiry(value: string | null) {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '未知'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function AppShell({ children }: PropsWithChildren) {
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  const navigate = useNavigate()
  const { session, isLoading } = usePlatformSession()
  const [accountOpen, setAccountOpen] = useState(false)
  const [copiedWallet, setCopiedWallet] = useState(false)
  const [accountError, setAccountError] = useState<string | null>(null)
  const [isLoggingOut, setIsLoggingOut] = useState(false)
  const accountRef = useRef<HTMLDivElement>(null)
  const accountTriggerRef = useRef<HTMLButtonElement>(null)

  const accountName = session.username || session.walletShort || '已登录用户'
  const methodLabel = authMethodLabel(session.authMethod, session.authType)

  useEffect(() => {
    if (!isLoading && !session.isAuthenticated) {
      void navigate({ to: '/', replace: true })
    }
  }, [isLoading, navigate, session])

  useEffect(() => {
    if (!accountOpen) return
    function handlePointerDown(event: PointerEvent) {
      if (!accountRef.current?.contains(event.target as Node)) setAccountOpen(false)
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setAccountOpen(false)
        accountTriggerRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [accountOpen])

  useEffect(() => {
    setAccountOpen(false)
    setAccountError(null)
  }, [pathname])

  async function handleCopyWallet() {
    if (!session.walletId) return
    try {
      await navigator.clipboard.writeText(session.walletId)
      setCopiedWallet(true)
      setAccountError(null)
      window.setTimeout(() => setCopiedWallet(false), 1800)
    } catch {
      setAccountError('无法访问剪贴板，请手动选择钱包地址复制。')
    }
  }

  async function handleLogout() {
    setIsLoggingOut(true)
    setAccountError(null)
    try {
      await logout()
      queryClient.clear()
      setAccountOpen(false)
      await navigate({ to: '/', replace: true })
    } catch (error) {
      setAccountError(error instanceof Error ? error.message : '退出登录失败，请检查连接后重试。')
    } finally {
      setIsLoggingOut(false)
    }
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
            <div className="topbar-subtitle">管理运行时与应用工作负载</div>
          </div>
          <div className="topbar-account" ref={accountRef}>
            <button
              ref={accountTriggerRef}
              type="button"
              className="account-trigger"
              aria-label={`账户：${accountName}`}
              aria-haspopup="dialog"
              aria-expanded={accountOpen}
              aria-controls={accountOpen ? 'account-popover' : undefined}
              onClick={() => {
                setAccountError(null)
                setAccountOpen((open) => !open)
              }}
            >
              <span className="account-avatar" aria-hidden="true">{Array.from(accountName)[0] || <UserRound className="h-4 w-4" />}</span>
              <span className="account-trigger-copy">
                <strong title={accountName}>{accountName}</strong>
                <span>{methodLabel}</span>
              </span>
              <ChevronDown className="account-chevron h-4 w-4" aria-hidden="true" />
            </button>
            {accountOpen && (
              <section className="account-popover" id="account-popover" role="dialog" aria-label="账户信息">
                <div className="account-popover-heading">
                  <span className="account-avatar account-avatar-large" aria-hidden="true">{Array.from(accountName)[0] || <UserRound className="h-5 w-5" />}</span>
                  <div className="account-popover-name">
                    <strong>{accountName}</strong>
                    <span>{methodLabel} · 已验证</span>
                  </div>
                </div>
                <div className="account-detail">
                  <span>钱包地址</span>
                  <div className="account-wallet-value">
                    <code>{session.walletId || '未提供'}</code>
                    {session.walletId && (
                      <button type="button" className="account-copy-button" onClick={() => void handleCopyWallet()}>
                        {copiedWallet ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                        {copiedWallet ? '已复制' : '复制地址'}
                      </button>
                    )}
                  </div>
                </div>
                <div className="account-detail">
                  <span>会话有效期至</span>
                  <strong>{formatSessionExpiry(session.expiresAt)}</strong>
                </div>
                {accountError && <div className="account-error" role="alert">{accountError}</div>}
                <button type="button" className="account-logout" onClick={() => void handleLogout()} disabled={isLoggingOut}>
                  <LogOut className="h-4 w-4" />
                  {isLoggingOut ? '正在退出…' : '退出登录'}
                </button>
              </section>
            )}
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
