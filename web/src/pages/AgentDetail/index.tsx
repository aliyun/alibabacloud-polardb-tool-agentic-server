import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Empty,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'

import {
  getAgent,
  regenerateAgentToken,
  revealAgentToken,
  revokeAgentToken,
  updateAgent,
  type Agent,
  type AgentToken,
  type AgentTokenStatus,
  type AgentTokenSummary,
} from '../../api/agents'
import { getAPIErrorMessage } from '../../api/client'
import { executeConfig } from '../../api/configuration'
import {
  listInstanceCredentials,
  type InstanceCredential,
} from '../../api/credentials'
import {
  createAgentInstanceAccess,
  deleteAgentInstanceAccess,
  listAgentResources,
  listAgentInstanceAccess,
  listInstances,
  updateAgentInstanceAccess,
  type AgentResource,
  type AgentInstanceAccess,
  type AgentInstanceAccessCapability,
  type AgentInstanceAccessInput,
  type InstanceSummary,
} from '../../api/instanceAccess'
import {
  listProvisioningBackends,
  type ProvisioningBackend,
} from '../../api/provisioningBackends'
import BindingEditor from '../../components/BindingEditor'
import PageContainer from '../../components/PageContainer'
import MCPConnectionPanel from './MCPConnectionPanel'

const { Text, Title } = Typography

const EMPTY_INSTANCE_ACCESS: AgentInstanceAccessInput = {
  instance_id: '',
  credential_id: null,
  permission: null,
  direct_enabled: null,
  capabilities: [],
}

const EDITABLE_ACCESS_CAPABILITIES =
  new Set<AgentInstanceAccessCapability>([
  'db_instance:list',
  'db_instance:describe',
  'db_instance:credentials:read',
  'sql:read',
  'sql:write',
  'db_instance:create',
])

function editableAccessCapabilities(
  capabilities: AgentInstanceAccess['capabilities'],
): AgentInstanceAccessCapability[] {
  return capabilities.filter(
    (capability): capability is AgentInstanceAccessCapability =>
      EDITABLE_ACCESS_CAPABILITIES.has(capability),
  )
}

function buildMCPServerURL(baseUrl: unknown): string {
  const configuredBaseUrl =
    typeof baseUrl === 'string' ? baseUrl.trim() : ''
  const effectiveBaseUrl = configuredBaseUrl || window.location.origin
  return `${effectiveBaseUrl.replace(/\/+$/, '')}/mcp`
}

type Confirmation =
  | { kind: 'status' }
  | { kind: 'regenerate' }
  | { kind: 'revoke' }
  | { kind: 'delete-access'; access: AgentInstanceAccess }
  | null

interface RouteScope {
  agentId: string
  generation: number
  mounted: boolean
}

function SectionHeading({
  id,
  title,
  description,
  action,
}: {
  id: string
  title: string
  description: string
  action?: React.ReactNode
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: 16,
        flexWrap: 'wrap',
      }}
    >
      <div>
        <Title id={id} level={4} style={{ marginBlock: 0 }}>
          {title}
        </Title>
        <Text type="secondary">{description}</Text>
      </div>
      {action}
    </div>
  )
}

export default function AgentDetail() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const scopeRef = useRef<RouteScope>({
    agentId: id,
    generation: 0,
    mounted: false,
  })
  const [agent, setAgent] = useState<Agent | null>(null)
  const [instances, setInstances] = useState<InstanceSummary[]>([])
  const [credentials, setCredentials] = useState<InstanceCredential[]>([])
  const [credentialLoadedIds, setCredentialLoadedIds] = useState<string[]>([])
  const [credentialLoadingId, setCredentialLoadingId] = useState<string | null>(
    null,
  )
  const [credentialErrors, setCredentialErrors] = useState<
    Record<string, string>
  >({})
  const [backends, setBackends] = useState<ProvisioningBackend[]>([])
  const [instanceAccess, setInstanceAccess] = useState<
    AgentInstanceAccess[]
  >([])
  const [resources, setResources] = useState<AgentResource[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation>(null)
  const [token, setToken] = useState<string | null>(null)
  const [tokenLoading, setTokenLoading] = useState(false)
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [mcpUrl, setMcpUrl] = useState(() =>
    buildMCPServerURL(window.location.origin),
  )
  const [accessDraft, setAccessDraft] =
    useState<AgentInstanceAccessInput | null>(null)
  const [accessDraftInstanceId, setAccessDraftInstanceId] =
    useState<string | null>(null)
  const [accessDraftValid, setAccessDraftValid] = useState(false)
  const [accessDeleteConflict, setAccessDeleteConflict] =
    useState<{ instanceId: string; resourceCount: number } | null>(null)
  const availableInstances = useMemo(() => {
    const boundInstanceIds = new Set(
      instanceAccess.map((access) => access.instance_id),
    )
    return instances.filter(
      (instance) => !boundInstanceIds.has(instance.id),
    )
  }, [instanceAccess, instances])
  const allRegisteredInstancesBound =
    instances.length > 0 && availableInstances.length === 0

  const isCurrentScope = useCallback((scope: RouteScope) => {
    const current = scopeRef.current
    return (
      current.mounted &&
      current.agentId === scope.agentId &&
      current.generation === scope.generation
    )
  }, [])

  const loadToken = useCallback(
    async (scope: RouteScope) => {
      setTokenLoading(true)
      setTokenError(null)
      try {
        const response = await revealAgentToken(scope.agentId, {
          confirmed: true,
        })
        if (!isCurrentScope(scope)) return
        if (!response.data.token) throw new Error('Agent Token is not active')
        setToken(response.data.token)
      } catch (requestError) {
        if (!isCurrentScope(scope)) return
        setToken(null)
        setTokenError(
          getAPIErrorMessage(
            requestError,
            'Could not load the active Agent Token.',
          ),
        )
      } finally {
        if (isCurrentScope(scope)) setTokenLoading(false)
      }
    },
    [isCurrentScope],
  )

  const load = useCallback(async (scope: RouteScope) => {
    try {
      const [
        agentResponse,
        instancesResponse,
        backendsResponse,
        accessResponse,
        resourcesResponse,
      ] = await Promise.all([
        getAgent(scope.agentId),
        listInstances(),
        listProvisioningBackends(),
        listAgentInstanceAccess(scope.agentId),
        listAgentResources(scope.agentId),
      ])
      if (!isCurrentScope(scope)) return
      const loadedAgent = agentResponse.data
      setAgent(loadedAgent)
      setInstances(instancesResponse.items)
      setBackends(backendsResponse.data)
      setInstanceAccess(accessResponse.data)
      setResources(
        resourcesResponse.data.filter((resource) => resource.status !== 'deleted'),
      )
      if (loadedAgent.token_summary?.status === 'active') {
        void loadToken(scope)
      } else {
        setToken(null)
        setTokenLoading(false)
        setTokenError(null)
      }
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(getAPIErrorMessage(requestError, 'Could not load this Agent.'))
    } finally {
      if (isCurrentScope(scope)) setLoading(false)
    }
  }, [isCurrentScope, loadToken])

  const loadMCPServerURL = useCallback(
    async (scope: RouteScope) => {
      let externalBaseUrl: unknown
      try {
        const response = await executeConfig({
          action: 'describe',
          module: 'runtime_policy',
        })
        externalBaseUrl =
          response.module?.effective?.config.external_base_url
      } catch {
        // The Agent page remains usable when runtime configuration is unavailable.
      }
      if (isCurrentScope(scope)) {
        setMcpUrl(buildMCPServerURL(externalBaseUrl))
      }
    },
    [isCurrentScope],
  )

  const beginRouteLoad = useCallback(() => {
    const generation = scopeRef.current.generation + 1
    const scope = { agentId: id, generation, mounted: true }
    scopeRef.current = scope
    setAgent(null)
    setInstances([])
    setCredentials([])
    setCredentialLoadedIds([])
    setCredentialLoadingId(null)
    setCredentialErrors({})
    setBackends([])
    setInstanceAccess([])
    setResources([])
    setLoading(true)
    setBusy(false)
    setError(null)
    setNotice(null)
    setConfirmation(null)
    setToken(null)
    setTokenLoading(false)
    setTokenError(null)
    setMcpUrl(buildMCPServerURL(window.location.origin))
    setAccessDraft(null)
    setAccessDraftInstanceId(null)
    setAccessDraftValid(false)
    setAccessDeleteConflict(null)
    if (id) {
      void load(scope)
      void loadMCPServerURL(scope)
    }
  }, [id, load, loadMCPServerURL])

  const loadCredentialsForInstance = useCallback(
    async (instanceId: string) => {
      if (!instanceId || credentialLoadedIds.includes(instanceId)) return
      const scope = { ...scopeRef.current }
      setCredentialLoadingId(instanceId)
      setCredentialErrors((current) => {
        const next = { ...current }
        delete next[instanceId]
        return next
      })
      try {
        const response = await listInstanceCredentials(instanceId)
        if (!isCurrentScope(scope)) return
        setCredentials((current) => [
          ...current.filter(
            (credential) => credential.instance_id !== instanceId,
          ),
          ...response.data,
        ])
        setCredentialLoadedIds((current) =>
          current.includes(instanceId) ? current : [...current, instanceId],
        )
      } catch (requestError) {
        if (!isCurrentScope(scope)) return
        setCredentialErrors((current) => ({
          ...current,
          [instanceId]: getAPIErrorMessage(
            requestError,
            'Could not load credentials for this instance.',
          ),
        }))
      } finally {
        if (isCurrentScope(scope)) {
          setCredentialLoadingId((current) =>
            current === instanceId ? null : current,
          )
        }
      }
    },
    [credentialLoadedIds, isCurrentScope],
  )

  const updateAccessDraft = useCallback(
    (draft: AgentInstanceAccessInput) => {
      setAccessDraft(draft)
      if (draft.instance_id) void loadCredentialsForInstance(draft.instance_id)
    },
    [loadCredentialsForInstance],
  )

  useLayoutEffect(() => {
    beginRouteLoad()
    const generation = scopeRef.current.generation
    return () => {
      if (scopeRef.current.generation === generation) {
        scopeRef.current = {
          agentId: scopeRef.current.agentId,
          generation: generation + 1,
          mounted: false,
        }
      }
    }
  }, [beginRouteLoad])

  const instanceNames = useMemo(
    () =>
      Object.fromEntries(instances.map((instance) => [instance.id, instance.name])),
    [instances],
  )
  const backendNames = useMemo(
    () =>
      Object.fromEntries(
        backends.map((backend) => [
          backend.id,
          instanceNames[backend.instance_id] ?? backend.instance_id,
        ]),
      ),
    [backends, instanceNames],
  )

  const showReconnectNotice = (summary: string) => {
    setNotice(
      `${summary} Reconnect the MCP client so it refreshes authentication and the available tool list.`,
    )
  }

  const tokenSummary = (
    row: AgentToken,
    status: AgentTokenStatus,
    fallback: AgentTokenSummary | null,
  ): AgentTokenSummary => ({
    id: row.id ?? fallback?.id ?? '',
    token_prefix: row.token_prefix ?? fallback?.token_prefix ?? '',
    status,
    expires_at: row.expires_at ?? fallback?.expires_at ?? null,
    revoked_at: row.revoked_at ?? fallback?.revoked_at ?? null,
    last_used_at: row.last_used_at ?? fallback?.last_used_at ?? null,
    created_at: row.created_at ?? fallback?.created_at ?? new Date().toISOString(),
    updated_at: row.updated_at ?? fallback?.updated_at ?? null,
  })

  const performConfirmation = async () => {
    if (!confirmation || !agent) return
    const scope = { ...scopeRef.current }
    const currentConfirmation = confirmation
    const currentAgent = agent
    setBusy(true)
    setError(null)
    setAccessDeleteConflict(null)
    try {
      if (currentConfirmation.kind === 'status') {
        const next = currentAgent.status === 'active' ? 'disabled' : 'active'
        const response = await updateAgent(currentAgent.id, { status: next })
        if (!isCurrentScope(scope)) return
        setAgent(response.data)
        showReconnectNotice('Agent status changed.')
      } else if (currentConfirmation.kind === 'regenerate') {
        const response = await regenerateAgentToken(currentAgent.id)
        if (!isCurrentScope(scope)) return
        if (!response.data.token) throw new Error('Token response was empty')
        setToken(response.data.token)
        setTokenError(null)
        setAgent((current) =>
          current
            ? {
                ...current,
                token_summary: tokenSummary(
                  response.data,
                  'active',
                  current.token_summary,
                ),
              }
            : current,
        )
        showReconnectNotice('Token regenerated; the old Token is invalid.')
      } else if (currentConfirmation.kind === 'revoke') {
        const response = await revokeAgentToken(currentAgent.id)
        if (!isCurrentScope(scope)) return
        setToken(null)
        setTokenError(null)
        setAgent((current) =>
          current
            ? {
                ...current,
                token_summary: tokenSummary(
                  response.data,
                  'revoked',
                  current.token_summary,
                ),
              }
            : current,
        )
        showReconnectNotice('Token revoked.')
      } else {
        await deleteAgentInstanceAccess(
          currentAgent.id,
          currentConfirmation.access.instance_id,
        )
        if (!isCurrentScope(scope)) return
        setInstanceAccess((current) =>
          current.filter(
            (item) =>
              item.instance_id !== currentConfirmation.access.instance_id,
          ),
        )
        showReconnectNotice('Instance access removed.')
      }
      setConfirmation(null)
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      const status = (
        requestError as { response?: { status?: number } }
      ).response?.status
      if (
        currentConfirmation.kind === 'delete-access' &&
        status === 409
      ) {
        setAccessDeleteConflict({
          instanceId: currentConfirmation.access.instance_id,
          resourceCount: resources.filter(
            (resource) =>
              resource.backend_id ===
                currentConfirmation.access.provisioning_backend_id &&
              resource.status !== 'deleted',
          ).length,
        })
        return
      }
      setError(
        getAPIErrorMessage(requestError, 'The requested change could not be saved.'),
      )
    } finally {
      if (isCurrentScope(scope)) setBusy(false)
    }
  }

  const saveInstanceAccess = async () => {
    if (!agent || !accessDraft || !accessDraftValid) return
    const scope = { ...scopeRef.current }
    const currentAgent = agent
    const currentDraft = accessDraft
    const currentInstanceId = accessDraftInstanceId
    setBusy(true)
    setError(null)
    try {
      const response = currentInstanceId
        ? await updateAgentInstanceAccess(
            currentAgent.id,
            currentInstanceId,
            {
            credential_id: currentDraft.credential_id,
            permission: currentDraft.permission,
            direct_enabled: currentDraft.direct_enabled,
            capabilities: currentDraft.capabilities,
            },
          )
        : await createAgentInstanceAccess(currentAgent.id, currentDraft)
      if (!isCurrentScope(scope)) return
      setInstanceAccess((current) =>
        currentInstanceId
          ? current.map((item) =>
              item.instance_id === response.data.instance_id
                ? response.data
                : item,
            )
          : [...current, response.data],
      )
      setAccessDraft(null)
      setAccessDraftInstanceId(null)
      showReconnectNotice('Instance access changed.')
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(
        getAPIErrorMessage(requestError, 'Could not save instance access.'),
      )
    } finally {
      if (isCurrentScope(scope)) setBusy(false)
    }
  }

  const disableManagedDatabaseCreation = async () => {
    if (!agent || confirmation?.kind !== 'delete-access') return
    const scope = { ...scopeRef.current }
    const currentAgent = agent
    const currentAccess = confirmation.access
    setBusy(true)
    setError(null)
    try {
      const response = await updateAgentInstanceAccess(
        currentAgent.id,
        currentAccess.instance_id,
        {
          credential_id: currentAccess.credential_id,
          permission: currentAccess.permission,
          direct_enabled: currentAccess.direct_enabled,
          capabilities: currentAccess.capabilities.filter(
            (capability) => capability !== 'db_instance:create',
          ),
        },
      )
      if (!isCurrentScope(scope)) return
      setInstanceAccess((current) =>
        current.map((item) =>
          item.instance_id === response.data.instance_id
            ? response.data
            : item,
        ),
      )
      setAccessDeleteConflict(null)
      setConfirmation(null)
      showReconnectNotice('Managed database creation disabled.')
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(
        getAPIErrorMessage(
          requestError,
          'Could not disable managed database creation.',
        ),
      )
    } finally {
      if (isCurrentScope(scope)) setBusy(false)
    }
  }

  if (loading) {
    return (
      <PageContainer title="Agent" description="Loading Agent access settings…">
        <Skeleton active paragraph={{ rows: 12 }} />
      </PageContainer>
    )
  }

  if (!agent) {
    return (
      <PageContainer title="Agent unavailable">
        <Alert
          type="error"
          showIcon
          role="alert"
          message={error ?? 'The Agent could not be found.'}
          action={
            <Button onClick={beginRouteLoad} size="small">
              Retry
            </Button>
          }
        />
      </PageContainer>
    )
  }

  return (
    <PageContainer
      title={agent.name}
      description="Manage identity, credential lifecycle, access, and provisioned resources."
      actions={
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/agents')}
        >
          All Agents
        </Button>
      }
    >
      <Space direction="vertical" size={24} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon role="alert" message={error} />}
        {notice && (
          <Alert
            type="info"
            showIcon
            closable
            role="status"
            message="Reconnect the MCP client"
            description={notice}
            onClose={() => setNotice(null)}
          />
        )}

        <section aria-labelledby="agent-identity-heading">
          <SectionHeading
            id="agent-identity-heading"
            title="Identity & status"
            description="The identity presented by this Agent to the MCP server."
            action={
              <Button
                danger={agent.status === 'active'}
                onClick={() => setConfirmation({ kind: 'status' })}
              >
                {agent.status === 'active' ? 'Disable Agent' : 'Enable Agent'}
              </Button>
            }
          />
          <Descriptions
            column={{ xs: 1, sm: 2 }}
            size="small"
            style={{ marginTop: 16 }}
          >
            <Descriptions.Item label="Name">{agent.name}</Descriptions.Item>
            <Descriptions.Item label="Status">
              <Tag color={agent.status === 'active' ? 'success' : 'default'}>
                {agent.status === 'active' ? 'Active' : 'Disabled'}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="Description">
              {agent.description ?? 'No description'}
            </Descriptions.Item>
            <Descriptions.Item label="Active resource limit">
              {agent.max_active_resources ?? 'System default'}
            </Descriptions.Item>
            <Descriptions.Item label="Agent ID">
              <Text code copyable>{agent.id}</Text>
            </Descriptions.Item>
          </Descriptions>
        </section>

        <Divider style={{ margin: 0 }} />

        <section aria-labelledby="agent-mcp-connection-heading">
          <MCPConnectionPanel
            agentName={agent.name}
            mcpUrl={mcpUrl}
            token={token}
            loading={tokenLoading}
            error={tokenError}
            tokenStatus={agent.token_summary?.status ?? null}
            expiresAt={agent.token_summary?.expires_at ?? null}
            lastUsedAt={agent.token_summary?.last_used_at ?? null}
            onRetry={() => void loadToken({ ...scopeRef.current })}
            onRegenerate={() => setConfirmation({ kind: 'regenerate' })}
            onRevoke={() => setConfirmation({ kind: 'revoke' })}
          />
        </section>

        <Divider style={{ margin: 0 }} />

        <section aria-labelledby="agent-instance-access-heading">
          <SectionHeading
            id="agent-instance-access-heading"
            title="Instance access"
            description="Grant direct SQL and metadata access, managed database creation, or both for each registered instance."
            action={
              <Button
                onClick={() => {
                  setAccessDraft({ ...EMPTY_INSTANCE_ACCESS })
                  setAccessDraftInstanceId(null)
                  setAccessDraftValid(false)
                }}
                disabled={
                  accessDraft !== null ||
                  availableInstances.length === 0
                }
              >
                Add instance access
              </Button>
            }
          />
          {allRegisteredInstancesBound && !accessDraft && (
            <Text type="secondary">
              All registered instances already have Agent access.
            </Text>
          )}
          {accessDraft && (
            <div
              style={{
                background: 'var(--surface-tertiary)',
                borderRadius: 'var(--radius-md)',
                padding: 16,
                marginTop: 16,
              }}
            >
              <Title level={5} style={{ marginTop: 0 }}>
                {accessDraftInstanceId
                  ? 'Edit instance access'
                  : 'New instance access'}
              </Title>
              <BindingEditor
                mode={accessDraftInstanceId ? 'edit' : 'create'}
                value={accessDraft}
                onChange={updateAccessDraft}
                onValidityChange={setAccessDraftValid}
                instances={
                  accessDraftInstanceId ? instances : availableInstances
                }
                credentials={credentials}
                provisioningBackends={backends}
                disabled={
                  busy ||
                  credentialLoadingId === accessDraft.instance_id
                }
              />
              {accessDraft.instance_id &&
                credentialErrors[accessDraft.instance_id] && (
                  <Alert
                    type="error"
                    showIcon
                    message={credentialErrors[accessDraft.instance_id]}
                  />
                )}
              <Space style={{ marginTop: 16 }}>
                <Button
                  type="primary"
                  disabled={!accessDraftValid}
                  loading={busy}
                  onClick={() => void saveInstanceAccess()}
                >
                  Save instance access
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => {
                    setAccessDraft(null)
                    setAccessDraftInstanceId(null)
                  }}
                >
                  Cancel
                </Button>
              </Space>
            </div>
          )}
          <Table
            rowKey="instance_id"
            dataSource={instanceAccess}
            pagination={false}
            scroll={{ x: 820 }}
            style={{ marginTop: 16 }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="No instance access. Add an instance to grant only the capabilities this Agent needs."
                />
              ),
            }}
            columns={[
              {
                title: 'Instance',
                dataIndex: 'instance_id',
                render: (instanceId: string) =>
                  instanceNames[instanceId] ?? instanceId,
              },
              {
                title: 'Permission',
                dataIndex: 'permission',
                render: (value: string | null) =>
                  value === 'readonly'
                    ? 'Read only'
                    : value === 'readwrite'
                      ? 'Read and write'
                      : '—',
              },
              {
                title: 'Capabilities',
                dataIndex: 'capabilities',
                render: (values: string[]) => (
                  <Space wrap>
                    {values.map((value) => <Tag key={value}>{value}</Tag>)}
                  </Space>
                ),
              },
              {
                title: 'State',
                key: 'state',
                render: (_: unknown, access: AgentInstanceAccess) => (
                  <Space wrap>
                    {access.direct_binding_id && (
                      <Tag
                        color={
                          access.direct_enabled ? 'success' : 'default'
                        }
                      >
                        Direct {access.direct_enabled ? 'enabled' : 'disabled'}
                      </Tag>
                    )}
                    {access.provisioning_binding_id && (
                      <Tag
                        color={
                          access.capabilities.includes('db_instance:create')
                            ? 'success'
                            : 'default'
                        }
                      >
                        Create{' '}
                        {access.capabilities.includes('db_instance:create')
                          ? 'enabled'
                          : 'disabled'}
                      </Tag>
                    )}
                  </Space>
                ),
              },
              {
                title: 'Actions',
                key: 'actions',
                render: (_: unknown, access: AgentInstanceAccess) => (
                  <Space>
                    <Button
                      size="small"
                      onClick={() => {
                        const draft = {
                          instance_id: access.instance_id,
                          credential_id: access.credential_id,
                          permission: access.permission,
                          direct_enabled: access.direct_enabled,
                          capabilities: editableAccessCapabilities(
                            access.capabilities,
                          ),
                        }
                        setAccessDraft(draft)
                        void loadCredentialsForInstance(access.instance_id)
                        setAccessDraftInstanceId(access.instance_id)
                        setAccessDraftValid(false)
                      }}
                    >
                      Edit
                    </Button>
                    <Button
                      size="small"
                      danger
                      onClick={() =>
                        setConfirmation({ kind: 'delete-access', access })
                      }
                    >
                      Remove
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </section>

        <Divider style={{ margin: 0 }} />

        <section aria-labelledby="agent-resources-heading">
          <SectionHeading
            id="agent-resources-heading"
            title="Resources"
            description="Non-terminal databases created by this Agent through provisioning backends."
          />
          <Table
            rowKey="id"
            dataSource={resources}
            pagination={false}
            scroll={{ x: 780 }}
            style={{ marginTop: 16 }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="This Agent does not own any active database resources."
                />
              ),
            }}
            columns={[
              { title: 'Name', dataIndex: 'name', render: (value) => value ?? '—' },
              { title: 'Engine', dataIndex: 'engine' },
              {
                title: 'Status',
                dataIndex: 'status',
                render: (status: string) => (
                  <Tag color={status === 'ready' ? 'success' : 'processing'}>
                    {status}
                  </Tag>
                ),
              },
              {
                title: 'Backend',
                dataIndex: 'backend_id',
                render: (backendId: string) =>
                  backendNames[backendId] ?? backendId,
              },
              { title: 'Client token', dataIndex: 'client_token' },
              {
                title: 'Created',
                dataIndex: 'created_at',
                render: (value: string) => new Date(value).toLocaleString(),
              },
            ]}
          />
        </section>
      </Space>

      <Modal
        title={
          confirmation?.kind === 'regenerate'
            ? 'Regenerate Agent Token?'
            : confirmation?.kind === 'revoke'
              ? 'Revoke Agent Token?'
              : confirmation?.kind === 'status'
                ? `${agent.status === 'active' ? 'Disable' : 'Enable'} Agent?`
                : 'Remove access?'
        }
        open={confirmation !== null}
        okText={
          confirmation?.kind === 'regenerate'
            ? 'Confirm regenerate'
            : confirmation?.kind === 'revoke'
              ? 'Confirm revoke'
              : confirmation?.kind === 'status'
                ? `Confirm ${agent.status === 'active' ? 'disable' : 'enable'}`
                : 'Confirm remove'
        }
        okButtonProps={{
          danger:
            confirmation?.kind !== 'status' || agent.status === 'active',
        }}
        confirmLoading={busy}
        onOk={() => void performConfirmation()}
        onCancel={() => {
          if (!busy) setConfirmation(null)
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12}>
          {confirmation?.kind === 'regenerate' && (
            <Alert
              type="warning"
              showIcon
              message="The old Token becomes invalid immediately."
              description="Update the intended MCP client with the new Token, then reconnect it."
            />
          )}
          {confirmation?.kind === 'revoke' && (
            <Alert
              type="warning"
              showIcon
              message="The Agent will not authenticate until a Token is regenerated."
            />
          )}
          {confirmation?.kind === 'status' && (
            <Text>
              This changes whether the Agent can authenticate and start new
              operations. Existing MCP sessions should reconnect afterward.
            </Text>
          )}
          {confirmation?.kind === 'delete-access' && (
            <Text>
              This access will be removed. Existing MCP sessions should
              reconnect to refresh their available tools.
            </Text>
          )}
          {confirmation?.kind === 'delete-access' &&
            accessDeleteConflict?.instanceId ===
              confirmation.access.instance_id && (
              <Alert
                type="warning"
                showIcon
                role="alert"
                message="This access still owns active resources"
                description={`The loaded resource list contains ${accessDeleteConflict.resourceCount} non-deleted resource${accessDeleteConflict.resourceCount === 1 ? '' : 's'} on this instance. Delete those resources before removing access, or disable managed database creation so no new resources can be created.`}
                action={
                  <Button
                    size="small"
                    loading={busy}
                    onClick={() =>
                      void disableManagedDatabaseCreation()
                    }
                  >
                    Disable managed database creation
                  </Button>
                }
              />
            )}
        </Space>
      </Modal>

    </PageContainer>
  )
}
