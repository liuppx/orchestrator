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
      void navigate({ to: '/agents', replace: true })
    }
  }, [navigate, session])

  async function handleLogin() {
    setSubmitting(true)
    setErrorText(null)
    setStatusText('正在请求钱包签名...')
    try {
      await walletLogin()
      setStatusText('登录成功，正在进入智能体列表...')
      await refetch()
      await navigate({ to: '/agents', replace: true })
    } catch (error) {
      setErrorText(errorMessage(error))
      setStatusText(null)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-slate-100 px-6 py-10">
      <div className="mx-auto max-w-3xl">
        <section className="rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
          <div className="mb-10 flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-sky-600 text-sm font-semibold text-white">
              HB
            </div>
            <div>
              <h1 className="text-2xl font-semibold text-slate-950">Hub</h1>
              <p className="text-sm text-slate-500">智能体控制面 React 工作区</p>
            </div>
          </div>
          <div className="space-y-4">
            <h2 className="text-3xl font-semibold tracking-tight text-slate-950">钱包登录入口</h2>
            <p className="max-w-2xl text-sm leading-6 text-slate-600">
              连接钱包后进入智能体工作区。当前页面只保留登录主流程和会话状态，不展示额外说明面板。
            </p>
          </div>
          <div className="mt-8 flex flex-wrap gap-3">
            <button
              type="button"
              onClick={() => void handleLogin()}
              disabled={submitting || isLoading}
              className="inline-flex h-11 items-center gap-2 rounded-lg bg-sky-600 px-4 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting || isLoading ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Wallet className="h-4 w-4" />}
              {submitting ? '等待签名' : '连接钱包'}
            </button>
            <button
              type="button"
              onClick={() => void refetch()}
              className="inline-flex h-11 items-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-medium text-slate-700"
            >
              刷新会话
            </button>
          </div>
          {statusText && (
            <div className="mt-4 inline-flex items-center gap-2 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
              <CheckCircle2 className="h-4 w-4" />
              {statusText}
            </div>
          )}
          {errorText && (
            <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {errorText}
            </div>
          )}
          {session.walletId && (
            <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
              当前会话：{shortWallet(session.walletId)}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
