import { useEffect, useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { ArrowUpRight, Blocks, Bot, CheckCircle2, Fingerprint, LoaderCircle, Server, ShieldCheck, Wallet } from 'lucide-react'

import { api, ApiError } from '../../platform/core/api'
import { usePlatformSession } from '../../platform/auth/queries'
import { shortWallet } from '../../platform/auth/format'

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
  const [cancellingPassport, setCancellingPassport] = useState(false)
  const passportPopupRef = useRef<Window | null>(null)

  function closePassportPopup() {
    if (passportPopupRef.current && !passportPopupRef.current.closed) passportPopupRef.current.close()
    passportPopupRef.current = null
  }

  useEffect(() => () => {
    const popup = passportPopupRef.current
    if (popup && !popup.closed) popup.close()
    passportPopupRef.current = null
  }, [])

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
          closePassportPopup()
          setPassportSession(null)
          setStatusText('身份已确认，正在进入运行概览...')
          await refetch()
          await navigate({ to: '/overview', replace: true })
        } else if (result.status === 'failed' || result.status === 'expired' || result.status === 'cancelled') {
          window.clearInterval(timer)
          closePassportPopup()
          setPassportSession(null)
          if (result.status === 'cancelled') {
            setStatusText('通行证验证已取消。')
          } else {
            setErrorText(result.message || (result.status === 'expired' ? '通行证登录已过期，请重新发起。' : '通行证身份验证失败。'))
            setStatusText(null)
          }
        }
      } catch (error) {
        window.clearInterval(timer)
        closePassportPopup()
        setPassportSession(null)
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
      const { getProvider, loginWithWalletIdentity } = await import('@yeying-community/web3-bs')
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
    if (submitting || cancellingPassport || passportSession) return
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
    passportPopupRef.current = popup
    try {
      const next = await api<PassportLoginSession>('/api/v1/public/auth/passport/sessions', { method: 'POST' })
      setPassportSession(next)
      popup.location.assign(next.verify_url)
      setStatusText('请在夜莺通行证页面完成验证。')
    } catch (error) {
      popup.close()
      passportPopupRef.current = null
      setPassportSession(null)
      setErrorText(errorMessage(error))
      setStatusText(null)
    } finally {
      setSubmitting(false)
    }
  }

  async function cancelPassportLogin(): Promise<boolean> {
    if (!passportSession || cancellingPassport) return !cancellingPassport
    setCancellingPassport(true)
    setErrorText(null)
    try {
      const result = await api<{ status: string }>(`/api/v1/public/auth/passport/sessions/${encodeURIComponent(passportSession.session_id)}`, { method: 'DELETE' })
      if (result.status === 'completed') {
        setStatusText('身份已确认，正在进入运行概览...')
        await api<PassportLoginStatus>(`/api/v1/public/auth/passport/sessions/${encodeURIComponent(passportSession.session_id)}`)
        closePassportPopup()
        setPassportSession(null)
        await refetch()
        await navigate({ to: '/overview', replace: true })
        return false
      }
      if (!['cancelled', 'failed', 'expired'].includes(result.status)) {
        setErrorText('当前验证仍在处理中，请稍后重试。')
        return false
      }
      closePassportPopup()
      setPassportSession(null)
      setStatusText(result.status === 'cancelled' ? '通行证验证已取消。' : '当前通行证请求已结束。')
      return true
    } catch (error) {
      setErrorText(`取消通行证验证失败：${errorMessage(error)}`)
      return false
    } finally {
      setCancellingPassport(false)
    }
  }

  async function selectMode(next: LoginMode) {
    if (next === mode || submitting || cancellingPassport) return
    if (passportSession && !(await cancelPassportLogin())) return
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
          <div><div className="brand-name">Orchestrator</div><div className="brand-caption">运行时编排控制面</div></div>
        </div>
        <div className="login-context-title">
          <h2>让每个运行实例，<br />都清晰可见。</h2>
          <p>连接机器 Hub，管理 OpenClaw Runtime，并在同一处查看实例状态、日志与应用工作台。</p>
          <div className="login-pipeline" aria-label="机器节点、运行时、应用工作台">
            <div className="login-pipeline-step">
              <span><Server className="h-3.5 w-3.5" />机器 Hub</span>
              <small>节点连接</small>
            </div>
            <span className="login-pipeline-line" aria-hidden="true" />
            <div className="login-pipeline-step">
              <span><Bot className="h-3.5 w-3.5" />Runtime</span>
              <small>实例运行</small>
            </div>
            <span className="login-pipeline-line" aria-hidden="true" />
            <div className="login-pipeline-step">
              <span><Blocks className="h-3.5 w-3.5" />工作台</span>
              <small>应用操作</small>
            </div>
          </div>
        </div>
        <div className="login-context-foot">当前节点能力以控制台实际检测结果为准。</div>
      </section>
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-panel-inner">
          <h1 id="login-title">登录运行控制台</h1>
          <p>选择钱包身份或夜莺通行证进入你的控制台。</p>
          <div className="login-actions">
            <div className="login-mode-switch" role="group" aria-label="选择登录方式">
              <button type="button" onClick={() => void selectMode('wallet')} disabled={submitting || cancellingPassport} aria-pressed={mode === 'wallet'} className={mode === 'wallet' ? 'active' : ''}>
                <Wallet className="h-4 w-4" /> 钱包身份
              </button>
              <button type="button" onClick={() => void selectMode('passport')} disabled={submitting || cancellingPassport} aria-pressed={mode === 'passport'} className={mode === 'passport' ? 'active' : ''}>
                <Fingerprint className="h-4 w-4" /> 夜莺通行证
              </button>
            </div>
            {mode === 'wallet' ? (
              <button type="button" onClick={() => void handleLogin()} disabled={submitting || isLoading} className="login-primary">
                {submitting || isLoading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Wallet className="h-4 w-4" />}
                {submitting ? '等待钱包确认' : '连接钱包并继续'}
              </button>
            ) : (
              <button type="button" onClick={() => void handlePassportLogin()} disabled={submitting || isLoading || cancellingPassport || Boolean(passportSession)} className="login-primary">
                {submitting || passportSession ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Fingerprint className="h-4 w-4" />}
                {submitting ? '正在创建登录请求' : passportSession ? '等待通行证验证' : '打开夜莺通行证'}
              </button>
            )}
            {mode === 'passport' && passportSession && <a className="login-passport-link" href={passportSession.verify_url} target="_blank" rel="noreferrer">验证页没有打开？在新窗口继续 <ArrowUpRight className="h-4 w-4" /></a>}
            {mode === 'passport' && passportSession && <button type="button" className="login-cancel" onClick={() => void cancelPassportLogin()} disabled={cancellingPassport}>{cancellingPassport ? '正在取消...' : '取消通行证验证'}</button>}
          </div>
          {statusText && <div className="login-status" role="status"><CheckCircle2 className="h-4 w-4" />{statusText}</div>}
          {errorText && <div className="login-error" role="alert">{errorText}</div>}
          {session.walletId && <div className="login-session">当前会话：{shortWallet(session.walletId)}</div>}
          <div className="login-trust-note">
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            <span>身份验证成功后会显示用户名与登录方式；账户菜单可查看钱包地址和会话有效期。</span>
          </div>
        </div>
      </section>
    </main>
  )
}
