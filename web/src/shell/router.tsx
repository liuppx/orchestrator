import { createRootRoute, createRoute, createRouter, Outlet } from '@tanstack/react-router'

import { OverviewPage } from '../apps/control/overview-page'
import { RuntimePage } from '../apps/control/runtime-page'
import { AgentsPage, LoginPage, MessengerPage, RouteContent, TraderHomePage, TraderRecordPage, TraderStrategyPage } from './route-components'
import { ShellLayout } from './shell-layout'

const rootRoute = createRootRoute({
  component: Outlet,
})

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: () => <RouteContent><LoginPage /></RouteContent>,
})

const shellRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'shell',
  component: ShellLayout,
})

const agentsRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents',
  component: () => <RouteContent><AgentsPage /></RouteContent>,
})

const overviewRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/overview',
  component: OverviewPage,
})

const runtimeRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/runtime',
  component: RuntimePage,
})

const traderRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents/trader',
  component: () => <RouteContent><TraderHomePage /></RouteContent>,
})

const traderStrategyRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents/trader/$strategyId',
  component: () => <RouteContent><TraderStrategyPage /></RouteContent>,
})

const traderRecordRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents/trader/$strategyId/records/$recordId',
  component: () => <RouteContent><TraderRecordPage /></RouteContent>,
})

const messengerRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: '/agents/messenger',
  component: () => <RouteContent><MessengerPage /></RouteContent>,
})

const routeTree = rootRoute.addChildren([
  loginRoute,
  shellRoute.addChildren([overviewRoute, runtimeRoute, agentsRoute, traderRoute, traderStrategyRoute, traderRecordRoute, messengerRoute]),
])

export const router = createRouter({ routeTree })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
