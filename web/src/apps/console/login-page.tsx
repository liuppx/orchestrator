import { useEffect, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { CheckCircle2, LoaderCircle, Wallet } from 'lucide-react'

import { walletLogin } from '../../platform/auth/mutations'
import { usePlatformSession } from '../../platform/auth/queries'
import { shortWallet } from '../../platform/auth/web3'
import { ApiError } from '../../platform/core/api'

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
  const [statusText, setStatusText] = useState<string | null>(null)
  const [errorText, setErrorText] = useState<string | null>(null)

  useEffect(() => {
    if (session.isAuthenticated) {
      void navigate({ to: '/overview', replace: true })
    }
  }, [navigate, session])

  async function handleLogin() {
    setSubmitting(true)
    setErrorText(null)
    setStatusText('正在请求钱包签名...')
    try {
      await walletLogin()
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
        <p>使用 YeYing 社区钱包验证身份。</p>
        <div className="login-actions">
          <button type="button" onClick={() => void handleLogin()} disabled={submitting || isLoading} className="login-primary">
            {submitting || isLoading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Wallet className="h-4 w-4" />}
            {submitting ? '等待签名' : '使用钱包登录'}
          </button>
          <button type="button" onClick={() => void refetch()} className="login-secondary">刷新会话</button>
        </div>
        {statusText && <div className="login-status" role="status"><CheckCircle2 className="h-4 w-4" />{statusText}</div>}
        {errorText && <div className="login-error" role="alert">{errorText}</div>}
        {session.walletId && <div className="login-session">当前会话：{shortWallet(session.walletId)}</div>}
      </section>
    </main>
  )
}
