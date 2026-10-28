import { useQuery } from '@tanstack/react-query'

import { api } from '../core/api'
import type { AuthSession } from '../core/types'
import { defaultPlatformSession, derivePlatformSession } from './session'

export function useAuthSession() {
  return useQuery({
    queryKey: ['auth', 'me'],
    queryFn: () => api<AuthSession>('/api/v1/public/auth/me'),
    retry: false,
  })
}

export function usePlatformSession() {
  const query = useAuthSession()
  return {
    ...query,
    providerId: 'cookie-wallet' as const,
    session: derivePlatformSession(query.data),
    fallbackSession: defaultPlatformSession,
  }
}
