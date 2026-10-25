import type { AuthSession, PlatformSession } from '../core/types'
import { shortWallet } from './web3'

export function derivePlatformSession(raw: AuthSession | null | undefined): PlatformSession {
  if (!raw?.wallet_id) {
    return {
      isAuthenticated: false,
      walletId: null,
      walletShort: null,
      chainId: null,
      authType: null,
      issuedAt: null,
      expiresAt: null,
      capability: {
        protocol: 'cookie-wallet',
        hasUcanSession: false,
        hasUcanSignature: false,
      },
      raw: null,
    }
  }

  const hasUcanSession = raw.ucan_session !== undefined && raw.ucan_session !== null
  const hasUcanSignature = raw.ucan_signature !== undefined && raw.ucan_signature !== null

  return {
    isAuthenticated: true,
    walletId: raw.wallet_id,
    walletShort: shortWallet(raw.wallet_id),
    chainId: raw.chain_id ?? null,
    authType: raw.auth_type ?? 'wallet_plugin',
    issuedAt: raw.issued_at,
    expiresAt: raw.expires_at,
    capability: {
      protocol: hasUcanSession || hasUcanSignature ? 'ucan' : 'cookie-wallet',
      hasUcanSession,
      hasUcanSignature,
    },
    raw,
  }
}

export const defaultPlatformSession = derivePlatformSession(null)
