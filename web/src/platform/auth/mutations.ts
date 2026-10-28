import type { AuthSession } from '../core/types'
import { api } from '../core/api'
import { derivePlatformSession } from './session'

export async function walletLogin(): Promise<AuthSession> {
  const { createCookieWalletAuthProvider } = await import('./provider')
  return createCookieWalletAuthProvider(derivePlatformSession).login()
}

export async function logout(): Promise<void> {
  await api('/api/v1/public/auth/logout', { method: 'POST' })
}
