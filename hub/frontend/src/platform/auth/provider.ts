import { api } from '../core/api'
import type { AuthSession, PlatformSession } from '../core/types'
import { connectWallet, saveWalletHistory } from './web3'
import { loginWithWalletIdentity } from '@yeying-community/web3-bs'

export type PlatformAuthProvider = {
  id: 'cookie-wallet' | 'ucan'
  getSession: () => Promise<AuthSession>
  deriveSession: (raw: AuthSession | null | undefined) => PlatformSession
  login: () => Promise<AuthSession>
  logout: () => Promise<void>
}

async function fetchCookieWalletSession(): Promise<AuthSession> {
  return api<AuthSession>('/api/v1/public/auth/me')
}

async function loginWithCookieWallet(): Promise<AuthSession> {
  const { provider } = await connectWallet()
  const result = await loginWithWalletIdentity({ provider, baseUrl: '/api/v1/public/auth', storeToken: false })
  const response = result.response as AuthSession
  const wallet = result.walletAddress || response.wallet_id
  if (!wallet) throw new Error('钱包身份未返回钱包账户')
  const session = response

  saveWalletHistory(wallet)
  return session
}

async function logoutCurrentSession(): Promise<void> {
  await api('/api/v1/public/auth/logout', {
    method: 'POST',
  })
}

export function createCookieWalletAuthProvider(
  deriveSession: (raw: AuthSession | null | undefined) => PlatformSession,
): PlatformAuthProvider {
  return {
    id: 'cookie-wallet',
    getSession: fetchCookieWalletSession,
    deriveSession,
    login: loginWithCookieWallet,
    logout: logoutCurrentSession,
  }
}
