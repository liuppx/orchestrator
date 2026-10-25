import {
  focusPendingApproval,
  getAccounts,
  getChainId,
  getProvider,
  requestAccounts,
  signMessage as signWalletSdkMessage,
} from '@yeying-community/web3-bs'
import type { Eip1193Provider } from '@yeying-community/web3-bs'

declare global {
  interface Window {
    ethereum?: Eip1193Provider
  }
}

export async function connectWallet() {
  const provider = await getProvider({ preferYeYing: true, timeoutMs: 1200 })
  if (provider) {
    const pending = await focusPendingApproval(provider).catch(() => ({ focused: false }))
    if (!pending.focused) {
      await requestAccounts({ provider })
    }
    const accounts = await getAccounts(provider)
    const wallet = accounts[0]
    if (!wallet) {
      throw new Error('钱包未返回账户')
    }
    const chainId = (await getChainId(provider)) ?? '1'
    return { wallet, chainId, provider }
  }

  if (!window.ethereum) {
    throw new Error('未检测到钱包扩展')
  }

  const accounts = (await window.ethereum.request({
    method: 'eth_requestAccounts',
  })) as string[]
  const wallet = accounts[0]
  if (!wallet) {
    throw new Error('钱包未返回账户')
  }
  const chainId = (await window.ethereum.request({ method: 'eth_chainId' })) as string
  return { wallet, chainId, provider: window.ethereum }
}

export async function signWalletMessage(args: {
  provider: Eip1193Provider
  wallet: string
  message: string
}): Promise<string> {
  const { provider, wallet, message } = args
  return signWalletSdkMessage({
    provider,
    address: wallet,
    message,
    method: 'personal_sign',
  })
}

export async function requestOptionalUcan(args: { provider: Eip1193Provider }) {
  let ucanSession: unknown = null
  let ucanSignature: unknown = null

  try {
    ucanSession = await args.provider.request({
      method: 'yeying_ucan_session',
      params: [{ audience: location.origin, ttl: 86400 }],
    })
  } catch {
    ucanSession = null
  }

  if (ucanSession) {
    try {
      ucanSignature = await args.provider.request({
        method: 'yeying_ucan_sign',
        params: [{ signingInput: JSON.stringify({ aud: location.origin, ts: Date.now() }) }],
      })
    } catch {
      ucanSignature = null
    }
  }

  return { ucanSession, ucanSignature }
}

export function loadWalletHistory(): string[] {
  try {
    const raw = localStorage.getItem('hub_wallets')
    const items = raw ? JSON.parse(raw) : []
    return Array.isArray(items) ? items.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function saveWalletHistory(wallet: string) {
  const items = [wallet, ...loadWalletHistory().filter((item) => item !== wallet)].slice(0, 6)
  localStorage.setItem('hub_wallets', JSON.stringify(items))
}

export function shortWallet(wallet: string): string {
  if (wallet.length <= 12) {
    return wallet
  }
  return `${wallet.slice(0, 6)}...${wallet.slice(-4)}`
}
