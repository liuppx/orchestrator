import { useQuery } from '@tanstack/react-query'

import { api } from '../core/api'
import type {
  BotInstanceDiagnoseResponse,
  BotInstanceListResponse,
  BotInstanceLogsResponse,
  AgentListResponse,
  AgentTypesResponse,
  AgentWorkspaceSummary,
} from '../core/types'

export function useAgents() {
  return useQuery({
    queryKey: ['agents'],
    queryFn: () => api<AgentListResponse>('/api/v1/public/agents'),
  })
}

export function useAgentWorkspaceSummary(agentKey: string) {
  return useQuery({
    queryKey: ['agent-workspace', agentKey],
    queryFn: () => api<AgentWorkspaceSummary>(`/api/v1/public/agents/${agentKey}/summary`),
    enabled: Boolean(agentKey),
  })
}

export function useMessengerInstances() {
  return useQuery({
    queryKey: ['messenger', 'instances'],
    queryFn: () => api<BotInstanceListResponse>('/api/v1/public/agent/instances'),
  })
}

export function useMessengerInstanceLogs(instanceId: string | null) {
  return useQuery({
    queryKey: ['messenger', 'instance-logs', instanceId],
    queryFn: () => api<BotInstanceLogsResponse>(`/api/v1/public/agent/instances/${instanceId}/logs?lines=260`),
    enabled: Boolean(instanceId),
    refetchInterval: 5000,
  })
}

export function useMessengerInstanceDiagnose(instanceId: string | null) {
  return useQuery({
    queryKey: ['messenger', 'instance-diagnose', instanceId],
    queryFn: () =>
      api<BotInstanceDiagnoseResponse>(`/api/v1/public/agent/instances/${instanceId}/diagnose?auto_recover=false`),
    enabled: Boolean(instanceId),
  })
}

export function useMessengerAgentTypes() {
  return useQuery({
    queryKey: ['messenger', 'agent-types'],
    queryFn: () => api<AgentTypesResponse>('/api/v1/public/agent/types'),
  })
}
