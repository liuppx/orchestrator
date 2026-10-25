import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../core/api'
import type {
  BotInstanceActionResponse,
  BotInstanceCreateRequest,
  BotInstancePairResponse,
  AgentWorkspaceActionResponse,
  AgentWorkspaceConfigUpdateResponse,
} from '../core/types'

async function runAgentAction(agentKey: string, action: 'run-once' | 'start' | 'stop' | 'diagnose' | 'init-config') {
  return api<AgentWorkspaceActionResponse>(`/api/v1/public/agents/${agentKey}/actions/${action}`, {
    method: 'POST',
  })
}

async function unlockAgentOrderGuard(
  agentKey: string,
  payload: {
    trading_day: string
    strategy_id: string
    symbol: string
    side: string
    reason: string
    actor?: string
  },
) {
  return api<AgentWorkspaceActionResponse>(`/api/v1/public/agents/${agentKey}/actions/unlock-order`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

async function updateAgentConfig(
  agentKey: string,
  payload: {
    broker: string
    strategy_id?: string | null
    strategy: Record<string, unknown>
  },
) {
  return api<AgentWorkspaceConfigUpdateResponse>(`/api/v1/public/agents/${agentKey}/config`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

async function runMessengerInstanceAction(instanceId: string, action: 'start' | 'stop') {
  return api<BotInstanceActionResponse>(`/api/v1/public/agent/instances/${instanceId}/${action}`, {
    method: 'POST',
  })
}

async function createMessengerInstance(payload: BotInstanceCreateRequest) {
  return api<BotInstanceActionResponse['instance']>('/api/v1/public/agent/instances', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

async function pairMessengerWhatsapp(instanceId: string) {
  return api<BotInstancePairResponse>(`/api/v1/public/agent/instances/${instanceId}/pair-whatsapp`, {
    method: 'POST',
  })
}

function invalidateAgentWorkspace(queryClient: ReturnType<typeof useQueryClient>, agentKey: string) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['agent-workspace', agentKey] }),
    queryClient.invalidateQueries({ queryKey: ['agents'] }),
  ])
}

function invalidateMessenger(queryClient: ReturnType<typeof useQueryClient>, instanceId?: string) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['messenger', 'instances'] }),
    queryClient.invalidateQueries({ queryKey: ['agent-workspace', 'messenger'] }),
    ...(instanceId ? [queryClient.invalidateQueries({ queryKey: ['messenger', 'instance-logs', instanceId] })] : []),
    queryClient.invalidateQueries({ queryKey: ['agents'] }),
  ])
}

export function useAgentAction(agentKey: string, action: 'run-once' | 'start' | 'stop' | 'diagnose' | 'init-config') {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: () => runAgentAction(agentKey, action),
    onSuccess: async () => {
      await invalidateAgentWorkspace(queryClient, agentKey)
    },
  })
}

export function useAgentConfigUpdate(agentKey: string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (payload: { broker: string; strategy_id?: string | null; strategy: Record<string, unknown> }) =>
      updateAgentConfig(agentKey, payload),
    onSuccess: async () => {
      await invalidateAgentWorkspace(queryClient, agentKey)
    },
  })
}

export function useAgentOrderUnlock(agentKey: string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (payload: {
      trading_day: string
      strategy_id: string
      symbol: string
      side: string
      reason: string
      actor?: string
    }) => unlockAgentOrderGuard(agentKey, payload),
    onSuccess: async () => {
      await invalidateAgentWorkspace(queryClient, agentKey)
    },
  })
}

export function useMessengerInstanceAction(instanceId: string | null, action: 'start' | 'stop') {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async () => {
      if (!instanceId) {
        throw new Error('未选择实例')
      }
      return runMessengerInstanceAction(instanceId, action)
    },
    onSuccess: async (_data) => {
      await invalidateMessenger(queryClient, instanceId ?? undefined)
    },
  })
}

export function useMessengerInstanceCreate() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (payload: BotInstanceCreateRequest) => createMessengerInstance(payload),
    onSuccess: async () => {
      await invalidateMessenger(queryClient)
    },
  })
}

export function useMessengerWhatsappPair(instanceId: string | null) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async () => {
      if (!instanceId) {
        throw new Error('未选择实例')
      }
      return pairMessengerWhatsapp(instanceId)
    },
    onSuccess: async () => {
      await invalidateMessenger(queryClient, instanceId ?? undefined)
    },
  })
}
