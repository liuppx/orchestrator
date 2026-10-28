import { lazy, Suspense, type ReactNode } from 'react'

export const LoginPage = lazy(() => import('../apps/console/login-page').then((module) => ({ default: module.LoginPage })))
export const AgentsPage = lazy(() => import('../apps/console/agents-page').then((module) => ({ default: module.AgentsPage })))
export const MessengerPage = lazy(() => import('../apps/messenger/messenger-page').then((module) => ({ default: module.MessengerPage })))
export const TraderHomePage = lazy(() => import('../apps/trader/trader-home-page').then((module) => ({ default: module.TraderHomePage })))
export const TraderRecordPage = lazy(() => import('../apps/trader/trader-record-page').then((module) => ({ default: module.TraderRecordPage })))
export const TraderStrategyPage = lazy(() => import('../apps/trader/trader-strategy-page').then((module) => ({ default: module.TraderStrategyPage })))

export function RouteContent({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div className="session-check">正在载入工作区…</div>}>{children}</Suspense>
}
