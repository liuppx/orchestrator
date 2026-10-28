export function shortWallet(wallet: string): string {
  if (wallet.length <= 12) {
    return wallet
  }
  return `${wallet.slice(0, 6)}...${wallet.slice(-4)}`
}
