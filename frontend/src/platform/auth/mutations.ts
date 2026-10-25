import type { AuthSession } from '../core/types'
import { activeAuthProvider } from './queries'

export async function walletLogin(): Promise<AuthSession> {
  return activeAuthProvider.login()
}

export async function logout(): Promise<void> {
  await activeAuthProvider.logout()
}
