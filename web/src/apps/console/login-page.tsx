import { useEffect, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { ArrowUpRight, CheckCircle2, Fingerprint, LoaderCircle, Wallet } from 'lucide-react'
import { getProvider, loginWithWalletIdentity } from '@yeying-community/web3-bs'

import { api, ApiError } from '../../platform/core/api'
import { usePlatformSession } from '../../platform/auth/queries'
import { shortWallet } from '../../platform/auth/web3'

type LoginMode = 'wallet' | 'passport'
type PassportLoginSession = { session_id: string; verify_url: string; poll_interval?: number }
type PassportLoginStatus = { status: string; message?: string }

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error) {
    return error.message
  }
  return '登录失败'
}

export function LoginPage() {
  const navigate = useNavigate()
  const { session, isLoading, refetch } = usePlatformSession()
  const [submitting, setSubmitting] = useState(false)
  const [mode, setMode] = useState<LoginMode>('wallet')
  const [passportSession, setPassportSession] = useState<PassportLoginSession | null>(null)
  const [statusText, setStatusText] = useState<string | null>(null)
  const [errorText, setErrorText] = useState<string | null>(null)

  useEffect(() => {
    if (session.isAuthenticated) {
      void navigate({ to: '/overview', replace: true })
    }
  }, [navigate, session])

  useEffect(() => {
    if (!passportSession || mode !== 'passport') return
    const timer = window.setInterval(async () => {
      try {
        const result = await api<PassportLoginStatus>(`/api/v1/public/auth/passport/sessions/${encodeURIComponent(passportSession.session_id)}`)
        if (result.status === 'completed') {
          window.clearInterval(timer)
          setPassportSession(null)
          setStatusText('身份已确认，正在进入运行概览...')
          await refetch()
          await navigate({ to: '/overview', replace: true })
        } else if (result.status === 'failed' || result.status === 'expired') {
          window.clearInterval(timer)
          setErrorText(result.message || (result.status === 'expired' ? '通行证登录已过期，请重新发起。' : '通行证身份验证失败。'))
          setStatusText(null)
        }
      } catch (error) {
        window.clearInterval(timer)
        setErrorText(errorMessage(error))
        setStatusText(null)
      }
    }, Math.max(1000, (passportSession.poll_interval ?? 2) * 1000))
    return () => window.clearInterval(timer)
  }, [mode, navigate, passportSession, refetch])

  async function handleLogin() {
    setSubmitting(true)
    setErrorText(null)
    setPassportSession(null)
    setStatusText('正在连接钱包身份...')
    try {
      const provider = await getProvider({ preferYeYing: true, timeoutMs: 3000 })
      if (!provider) throw new Error('未检测到夜莺钱包或兼容钱包扩展。')
      await loginWithWalletIdentity({ baseUrl: '/api/v1/public/auth', sessionPath: 'identity/login/session?avatar=0', provider, storeToken: false })
      setStatusText('登录成功，正在进入运行概览...')
      await refetch()
      await navigate({ to: '/overview', replace: true })
    } catch (error) {
      setErrorText(errorMessage(error))
      setStatusText(null)
    } finally {
      setSubmitting(false)
    }
  }

  async function handlePassportLogin() {
    if (submitting) return
    setSubmitting(true)
    setErrorText(null)
    setStatusText('正在创建夜莺通行证登录请求...')
    const popup = window.open('about:blank', 'orchestrator-passkey-login')
    if (!popup) {
      setErrorText('浏览器阻止了验证窗口，请允许弹出窗口后重试。')
      setStatusText(null)
      setSubmitting(false)
      return
    }
    try {
      const next = await api<PassportLoginSession>('/api/v1/public/auth/passport/sessions', { method: 'POST' })
      setPassportSession(next)
      popup.location.assign(next.verify_url)
      setStatusText('请在夜莺通行证页面完成验证。')
    } catch (error) {
      popup.close()
      setErrorText(errorMessage(error))
      setStatusText(null)
    } finally {
      setSubmitting(false)
    }
  }

  function selectMode(next: LoginMode) {
    if (next === mode) return
    setMode(next)
    setPassportSession(null)
    setErrorText(null)
    setStatusText(null)
  }

  return (
    <main className="login-page">
      <section className="login-context" aria-label="产品介绍">
        <div className="brand-lockup">
          <div className="brand-mark">OR</div>
          <div><div className="brand-name">Orchestrator</div><div className="brand-caption">Runtime Control Plane</div></div>
        </div>
        <div className="login-context-title">
          <h2>管理本机<br />OpenClaw Runtime。</h2>
          <p>创建和维护 OpenClaw 实例，查看进程状态、运行日志与诊断结果。</p>
        </div>
        <div className="login-context-foot">当前节点能力以控制台实际检测结果为准。</div>
      </section>
      <section className="login-panel" aria-labelledby="login-title">
        <h1 id="login-title">登录运行控制台</h1>
        <p>通过钱包身份或夜莺通行证安全登录。</p>
        <div className="login-actions">
          <div className="login-mode-switch" role="group" aria-label="选择登录方式">
            <button type="button" onClick={() => selectMode('wallet')} aria-pressed={mode === 'wallet'} className={mode === 'wallet' ? 'active' : ''}>
              <Wallet className="h-4 w-4" /> 钱包身份
            </button>
            <button type="button" onClick={() => selectMode('passport')} aria-pressed={mode === 'passport'} className={mode === 'passport' ? 'active' : ''}>
              <Fingerprint className="h-4 w-4" /> 夜莺通行证
            </button>
          </div>
          {mode === 'wallet' ? (
            <button type="button" onClick={() => void handleLogin()} disabled={submitting || isLoading} className="login-primary">
              {submitting || isLoading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Wallet className="h-4 w-4" />}
              {submitting ? '等待钱包确认' : '连接钱包并继续'}
            </button>
          ) : (
            <button type="button" onClick={() => void handlePassportLogin()} disabled={submitting || isLoading} className="login-primary">
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Fingerprint className="h-4 w-4" />}
              {passportSession ? '重新发起通行证登录' : '打开夜莺通行证'}
            </button>
          )}
          {mode === 'passport' && passportSession && <a className="login-passport-link" href={passportSession.verify_url} target="_blank" rel="noreferrer">验证页没有打开？在新窗口继续 <ArrowUpRight className="h-4 w-4" /></a>}
          <button type="button" onClick={() => void refetch()} className="login-secondary">刷新会话</button>
        </div>
        {statusText && <div className="login-status" role="status"><CheckCircle2 className="h-4 w-4" />{statusText}</div>}
        {errorText && <div className="login-error" role="alert">{errorText}</div>}
        {session.walletId && <div className="login-session">当前会话：{shortWallet(session.walletId)}</div>}
      </section>
    </main>
  )
}
