import { useQuery } from '@tanstack/react-query'

import type { AuthSession } from '../core/types'
import { createCookieWalletAuthProvider } from './provider'
import { defaultPlatformSession, derivePlatformSession } from './session'

export const activeAuthProvider = createCookieWalletAuthProvider(derivePlatformSession)

export function useAuthSession() {
  return useQuery({
    queryKey: ['auth', 'me'],
    queryFn: () => activeAuthProvider.getSession() as Promise<AuthSession>,
    retry: false,
  })
}

export function usePlatformSession() {
  const query = useAuthSession()
  return {
    ...query,
    providerId: activeAuthProvider.id,
    session: activeAuthProvider.deriveSession(query.data),
    fallbackSession: defaultPlatformSession,
  }
}
