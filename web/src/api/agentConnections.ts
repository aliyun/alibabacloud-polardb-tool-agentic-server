import api from './client'

export interface MyAgentTokenSummary {
  token_prefix: string
  status: 'active' | 'revoked' | 'expired'
  expires_at: string | null
  last_used_at: string | null
}

export interface MyAgentConnection {
  assignment_id: string | null
  agent_id: string
  agent_name: string
  agent_status: 'active' | 'disabled'
  polarrag_instances: { id: string; name: string }[]
  password_reveal_available: boolean
  token: MyAgentTokenSummary | null
}

export interface MyAgentToken extends MyAgentTokenSummary {
  assignment_id: string
  token: string | null
}

const path = (connectionId: string, operation: string) =>
  `/api/me/agent-connections/${encodeURIComponent(connectionId)}/token/${operation}`

export const listMyAgentConnections = () =>
  api.get<MyAgentConnection[]>('/api/me/agent-connections')

export const issueMyAgentToken = (
  connectionId: string,
  expiresAt?: string,
) =>
  api.post<MyAgentToken>(
    path(connectionId, 'issue'),
    expiresAt ? { expires_at: expiresAt } : {},
  )

export const revealMyAgentToken = (connectionId: string, password: string) =>
  api.post<MyAgentToken>(
    path(connectionId, 'reveal'),
    { password },
    { pasSkipAuthRedirect: true },
  )

export const regenerateMyAgentToken = (
  connectionId: string,
  expiresAt?: string,
) =>
  api.post<MyAgentToken>(path(connectionId, 'regenerate'), {
    confirmed: true,
    ...(expiresAt ? { expires_at: expiresAt } : {}),
  })

export const revokeMyAgentToken = (connectionId: string) =>
  api.post<MyAgentToken>(path(connectionId, 'revoke'))
