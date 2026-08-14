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
  Radio,
  Select,
  Skeleton,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { ArrowLeftOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import {
  deleteAgentPolarRAGBinding,
  getAgent,
  listAgentPolarRAGBindings,
  listAgentPolarRAGPublicResources,
  regenerateAgentToken,
  revealAgentToken,
  revokeAgentToken,
  updateAgent,
  updateAgentPolarRAGPublicResources,
  type Agent,
  type AgentPolarRAGBinding,
  type AgentPolarRAGPublicResource,
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
import RESTAPIConnectionPanel from './RESTAPIConnectionPanel'
import DedicatedPoolRoutes from './DedicatedPoolRoutes'
import PolarRAGAccessPanel from './PolarRAGAccessPanel'

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

function buildServerBaseURL(baseUrl: unknown): string {
  const configuredBaseUrl =
    typeof baseUrl === 'string' ? baseUrl.trim() : ''
  const effectiveBaseUrl = configuredBaseUrl || window.location.origin
  return effectiveBaseUrl.replace(/\/+$/, '')
}

type Confirmation =
  | { kind: 'status' }
  | { kind: 'regenerate' }
  | { kind: 'revoke' }
  | { kind: 'delete-access'; access: AgentInstanceAccess }
  | { kind: 'delete-polarrag-access'; binding: AgentPolarRAGBinding }
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
  const { t } = useTranslation()
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
  const [polarRAGBindings, setPolarRAGBindings] = useState<
    AgentPolarRAGBinding[]
  >([])
  const [publicScopeBinding, setPublicScopeBinding] =
    useState<AgentPolarRAGBinding | null>(null)
  const [publicScopeOptions, setPublicScopeOptions] = useState<
    AgentPolarRAGPublicResource[]
  >([])
  const [publicScopeDraft, setPublicScopeDraft] = useState<string[] | null>(null)
  const [publicScopeLoading, setPublicScopeLoading] = useState(false)
  const [resources, setResources] = useState<AgentResource[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<Confirmation>(null)
  const [serverBaseUrl, setServerBaseUrl] = useState(() =>
    buildServerBaseURL(window.location.origin),
  )
  const mcpUrl = `${serverBaseUrl}/mcp`
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

  const load = useCallback(async (scope: RouteScope) => {
    try {
      const [
        agentResponse,
        instancesResponse,
        backendsResponse,
        accessResponse,
        polarRAGBindingsResponse,
        resourcesResponse,
      ] = await Promise.all([
        getAgent(scope.agentId),
        listInstances(),
        listProvisioningBackends(),
        listAgentInstanceAccess(scope.agentId),
        listAgentPolarRAGBindings(scope.agentId),
        listAgentResources(scope.agentId),
      ])
      if (!isCurrentScope(scope)) return
      const loadedAgent = agentResponse.data
      setAgent(loadedAgent)
      setInstances(instancesResponse.items)
      setBackends(backendsResponse.data)
      setInstanceAccess(accessResponse.data)
      setPolarRAGBindings(polarRAGBindingsResponse.data)
      setResources(
        resourcesResponse.data.filter((resource) => resource.status !== 'deleted'),
      )
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(getAPIErrorMessage(requestError, t('agentDetail.loadFailed')))
    } finally {
      if (isCurrentScope(scope)) setLoading(false)
    }
  }, [isCurrentScope, t])

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
        setServerBaseUrl(buildServerBaseURL(externalBaseUrl))
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
    setPolarRAGBindings([])
    setPublicScopeBinding(null)
    setPublicScopeOptions([])
    setPublicScopeDraft(null)
    setPublicScopeLoading(false)
    setResources([])
    setLoading(true)
    setBusy(false)
    setError(null)
    setNotice(null)
    setConfirmation(null)
    setServerBaseUrl(buildServerBaseURL(window.location.origin))
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
            t('agentDetail.credentialLoadFailed'),
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
    [credentialLoadedIds, isCurrentScope, t],
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
        backends.map((backend) => {
          const targetId =
            backend.instance_id ?? backend.dedicated_pool_id
          return [
            backend.id,
            (targetId && instanceNames[targetId]) ??
              targetId ??
              backend.id,
          ]
        }),
      ),
    [backends, instanceNames],
  )
  const showReconnectNotice = (summary: string) => {
    setNotice(
      `${summary} Reconnect the MCP client so it refreshes authentication and the available tool list.`,
    )
  }

  const openPublicScope = async (binding: AgentPolarRAGBinding) => {
    const scope = { ...scopeRef.current }
    setPublicScopeBinding(binding)
    setPublicScopeDraft(binding.public_knowledge_resource_ids)
    setPublicScopeOptions([])
    setPublicScopeLoading(true)
    setError(null)
    try {
      const response = await listAgentPolarRAGPublicResources(
        scope.agentId,
        binding.id,
      )
      if (isCurrentScope(scope)) setPublicScopeOptions(response.data)
    } catch (requestError) {
      if (isCurrentScope(scope)) {
        setError(
          getAPIErrorMessage(
            requestError,
            t('agentDetail.publicScopeLoadFailed'),
          ),
        )
        setPublicScopeBinding(null)
      }
    } finally {
      if (isCurrentScope(scope)) setPublicScopeLoading(false)
    }
  }

  const savePublicScope = async () => {
    if (!publicScopeBinding) return
    const scope = { ...scopeRef.current }
    setBusy(true)
    setError(null)
    try {
      const response = await updateAgentPolarRAGPublicResources(
        scope.agentId,
        publicScopeBinding.id,
        publicScopeDraft,
      )
      if (!isCurrentScope(scope)) return
      setPolarRAGBindings((current) =>
        current.map((binding) =>
          binding.id === response.data.id ? response.data : binding,
        ),
      )
      setNotice(t('agentDetail.publicScopeUpdated'))
      setPublicScopeBinding(null)
    } catch (requestError) {
      if (isCurrentScope(scope)) {
        setError(
          getAPIErrorMessage(
            requestError,
            t('agentDetail.publicScopeSaveFailed'),
          ),
        )
      }
    } finally {
      if (isCurrentScope(scope)) setBusy(false)
    }
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
        showReconnectNotice(t('agentDetail.statusChanged'))
      } else if (currentConfirmation.kind === 'regenerate') {
        const response = await regenerateAgentToken(currentAgent.id)
        if (!isCurrentScope(scope)) return
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
        showReconnectNotice(t('agentDetail.tokenRegenerated'))
      } else if (currentConfirmation.kind === 'revoke') {
        const response = await revokeAgentToken(currentAgent.id)
        if (!isCurrentScope(scope)) return
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
      } else if (currentConfirmation.kind === 'delete-access') {
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
      } else if (currentConfirmation.kind === 'delete-polarrag-access') {
        await deleteAgentPolarRAGBinding(
          currentAgent.id,
          currentConfirmation.binding.id,
        )
        if (!isCurrentScope(scope)) return
        setPolarRAGBindings((current) =>
          current.filter((item) => item.id !== currentConfirmation.binding.id),
        )
        showReconnectNotice('PolarRAG instance access removed.')
      } else {
        throw new Error('Unsupported confirmation action')
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
        getAPIErrorMessage(requestError, t('agentDetail.saveFailed')),
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
      showReconnectNotice(t('agentDetail.accessChanged'))
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(
        getAPIErrorMessage(requestError, t('agentDetail.accessSaveFailed')),
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
      showReconnectNotice(t('agentDetail.creationDisabled'))
    } catch (requestError) {
      if (!isCurrentScope(scope)) return
      setError(
        getAPIErrorMessage(
          requestError,
          t('agentDetail.creationDisableFailed'),
        ),
      )
    } finally {
      if (isCurrentScope(scope)) setBusy(false)
    }
  }

  if (loading) {
    return (
      <PageContainer title={t('agents.agent')} description={t('agentDetail.loading')}>
        <Skeleton active paragraph={{ rows: 12 }} />
      </PageContainer>
    )
  }

  if (!agent) {
    return (
      <PageContainer title={t('agentDetail.unavailable')}>
        <Alert
          type="error"
          showIcon
          role="alert"
          message={error ?? t('agentDetail.notFound')}
          action={
            <Button onClick={beginRouteLoad} size="small">
              {t('common.retry')}
            </Button>
          }
        />
      </PageContainer>
    )
  }

  const renderMCPConnectionPanel = (headingId: string) => (
    <section aria-labelledby={headingId}>
      <MCPConnectionPanel
        agentName={agent.name}
        headingId={headingId}
        mcpUrl={mcpUrl}
        tokenPrefix={agent.token_summary?.token_prefix ?? null}
        tokenStatus={agent.token_summary?.status ?? null}
        expiresAt={agent.token_summary?.expires_at ?? null}
        lastUsedAt={agent.token_summary?.last_used_at ?? null}
        revealToken={async (password) => {
          const response = await revealAgentToken(agent.id, { password })
          if (!response.data.token) {
            throw new Error('Agent Token is not active')
          }
          return response.data.token
        }}
        onRegenerate={() => setConfirmation({ kind: 'regenerate' })}
        onRevoke={() => setConfirmation({ kind: 'revoke' })}
      />
    </section>
  )

  return (
    <PageContainer
      title={agent.name}
      description={t('agentDetail.description')}
      actions={
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/agents')}
        >
          {t('agentDetail.allAgents')}
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
            message={t('agents.reconnect')}
            description={notice}
            onClose={() => setNotice(null)}
          />
        )}

        <section aria-labelledby="agent-identity-heading">
          <SectionHeading
            id="agent-identity-heading"
            title={t('agentDetail.identityTitle')}
            description={t('agentDetail.identityDescription')}
            action={
              <Button
                danger={agent.status === 'active'}
                onClick={() => setConfirmation({ kind: 'status' })}
              >
                {agent.status === 'active' ? t('agentDetail.disableAgent') : t('agentDetail.enableAgent')}
              </Button>
            }
          />
          <Descriptions
            column={{ xs: 1, sm: 2 }}
            size="small"
            style={{ marginTop: 16 }}
          >
            <Descriptions.Item label={t('agents.name')}>{agent.name}</Descriptions.Item>
            <Descriptions.Item label={t('agents.status')}>
              <Tag color={agent.status === 'active' ? 'success' : 'default'}>
                {agent.status === 'active' ? t('agents.active') : t('agents.disabled')}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('agents.descriptionLabel')}>
              {agent.description ?? t('agentDetail.noDescription')}
            </Descriptions.Item>
            <Descriptions.Item label={t('agentDetail.activeResourceLimit')}>
              {agent.max_active_resources ?? t('agents.systemDefault')}
            </Descriptions.Item>
            <Descriptions.Item label={t('agentDetail.agentId')}>
              <Text code copyable>{agent.id}</Text>
            </Descriptions.Item>
          </Descriptions>
        </section>

        <Divider style={{ margin: 0 }} />

        <Tabs
          defaultActiveKey="database"
          items={[
            {
              key: 'database',
              label: t('agentDetail.databaseTab'),
              children: (
                <Space direction="vertical" size={24} style={{ width: '100%' }}>
                  {renderMCPConnectionPanel(
                    'agent-database-mcp-connection-heading',
                  )}

                  <Divider style={{ margin: 0 }} />

                  <section aria-labelledby="agent-rest-api-heading">
                    <RESTAPIConnectionPanel serverBaseUrl={serverBaseUrl} />
                  </section>

                  <Divider style={{ margin: 0 }} />

                  <section aria-labelledby="agent-instance-access-heading">
                    <SectionHeading
                      id="agent-instance-access-heading"
                      title={t('agentDetail.accessTitle')}
                      description={t('agentDetail.accessDescription')}
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
                          {t('agentDetail.addAccess')}
                        </Button>
                      }
                    />
                    {allRegisteredInstancesBound && !accessDraft && (
                      <Text type="secondary">
                        {t('agentDetail.allHaveAccess')}
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
                            ? t('agentDetail.editAccess')
                            : t('agentDetail.newAccess')}
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
                            {t('agentDetail.saveAccess')}
                          </Button>
                          <Button
                            disabled={busy}
                            onClick={() => {
                              setAccessDraft(null)
                              setAccessDraftInstanceId(null)
                            }}
                          >
                            {t('common.cancel')}
                          </Button>
                        </Space>
                      </div>
                    )}
                    <Table
                      rowKey="instance_id"
                      dataSource={instanceAccess}
                      pagination={false}
                      scroll={{ x: 720 }}
                      style={{ marginTop: 16 }}
                      locale={{
                        emptyText: (
                          <Empty
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                            description={t('agentDetail.noAccess')}
                          />
                        ),
                      }}
                      columns={[
                        {
                          title: t('instances.instance'),
                          dataIndex: 'instance_id',
                          render: (instanceId: string) =>
                            instanceNames[instanceId] ?? instanceId,
                        },
                        {
                          title: t('agentDetail.permission'),
                          dataIndex: 'permission',
                          render: (permission: AgentInstanceAccess['permission']) =>
                            permission === 'readonly'
                              ? t('users.readOnly')
                              : permission === 'readwrite'
                                ? t('users.readWrite')
                                : '—',
                        },
                        {
                          title: t('agentDetail.capabilities'),
                          dataIndex: 'capabilities',
                          render: (capabilities: string[]) => (
                            <Space wrap>
                              {capabilities.map((value) => (
                                <Tag key={value}>{value}</Tag>
                              ))}
                            </Space>
                          ),
                        },
                        {
                          title: t('agentDetail.state'),
                          key: 'state',
                          render: (_: unknown, access: AgentInstanceAccess) => (
                            <Space wrap>
                              {access.direct_binding_id && (
                                <Tag
                                  color={
                                    access.direct_enabled ? 'success' : 'default'
                                  }
                                >
                                  {t('agentDetail.direct')}{' '}
                                  {access.direct_enabled
                                    ? t('common.enabled')
                                    : t('common.disabled')}
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
                                  {t('agentDetail.create')}{' '}
                                  {access.capabilities.includes('db_instance:create')
                                    ? t('common.enabled')
                                    : t('common.disabled')}
                                </Tag>
                              )}
                            </Space>
                          ),
                        },
                        {
                          title: t('instances.actions'),
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
                                {t('agentDetail.edit')}
                              </Button>
                              <Button
                                size="small"
                                danger
                                onClick={() =>
                                  setConfirmation({
                                    kind: 'delete-access',
                                    access,
                                  })
                                }
                              >
                                {t('agentDetail.remove')}
                              </Button>
                            </Space>
                          ),
                        },
                      ]}
                    />
                  </section>

                  <Divider style={{ margin: 0 }} />

                  <DedicatedPoolRoutes
                    agentId={agent.id}
                    resources={resources}
                    backends={backends}
                  />

                  <Divider style={{ margin: 0 }} />

                  <section aria-labelledby="agent-resources-heading">
                    <SectionHeading
                      id="agent-resources-heading"
                      title={t('agentDetail.resourcesTitle')}
                      description={t('agentDetail.resourcesDescription')}
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
                            description={t('agentDetail.noResources')}
                          />
                        ),
                      }}
                      columns={[
                        { title: t('instances.name'), dataIndex: 'name', render: (value) => value ?? '—' },
                        { title: t('instances.engine'), dataIndex: 'engine' },
                        {
                          title: t('instances.status'),
                          dataIndex: 'status',
                          render: (status: string) => (
                            <Tag color={status === 'ready' ? 'success' : 'processing'}>
                              {status}
                            </Tag>
                          ),
                        },
                        {
                          title: t('agentDetail.backend'),
                          dataIndex: 'backend_id',
                          render: (backendId: string) =>
                            backendNames[backendId] ?? backendId,
                        },
                        { title: t('agentDetail.clientToken'), dataIndex: 'client_token' },
                        {
                          title: t('agents.created'),
                          dataIndex: 'created_at',
                          render: (value: string) => new Date(value).toLocaleString(),
                        },
                      ]}
                    />
                  </section>
                </Space>
              ),
            },
            {
              key: 'polarrag',
              label: t('agentDetail.polarragTab'),
              children: (
                <Space direction="vertical" size={24} style={{ width: '100%' }}>
                  {renderMCPConnectionPanel(
                    'agent-polarrag-mcp-connection-heading',
                  )}

                  <Divider style={{ margin: 0 }} />

                  <section aria-labelledby="agent-polarrag-bindings-heading">
                    <SectionHeading
                      id="agent-polarrag-bindings-heading"
                      title={t('agentDetail.polarragBindingsTitle')}
                      description={t('agentDetail.polarragBindingsDescription')}
                    />
                    <Table
                      rowKey="id"
                      dataSource={polarRAGBindings}
                      pagination={false}
                      style={{ marginTop: 16 }}
                      locale={{
                        emptyText: (
                          <Empty
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                            description={t('agentDetail.noPolarragBindings')}
                          />
                        ),
                      }}
                      columns={[
                        {
                          title: t('instances.instance'),
                          dataIndex: 'instance_name',
                        },
                        {
                          title: t('agentDetail.permission'),
                          key: 'permission',
                          render: () => t('agentDetail.userAcl'),
                        },
                        {
                          title: t('agentDetail.publicScope'),
                          key: 'publicScope',
                          render: (_: unknown, binding: AgentPolarRAGBinding) =>
                            binding.public_knowledge_resource_ids === null
                              ? t('agentDetail.allPublicResources')
                              : t('agentDetail.selectedPublicResources', {
                                  count:
                                    binding.public_knowledge_resource_ids.length,
                                }),
                        },
                        {
                          title: t('agentDetail.state'),
                          key: 'state',
                          render: () => (
                            <Tag color="success">{t('agentDetail.bound')}</Tag>
                          ),
                        },
                        {
                          title: t('instances.actions'),
                          key: 'actions',
                          render: (_: unknown, binding: AgentPolarRAGBinding) => (
                            <Space>
                              <Button
                                size="small"
                                onClick={() => void openPublicScope(binding)}
                              >
                                {t('agentDetail.configurePublicScope')}
                              </Button>
                              <Button
                                size="small"
                                danger
                                onClick={() =>
                                  setConfirmation({
                                    kind: 'delete-polarrag-access',
                                    binding,
                                  })
                                }
                              >
                                {t('agentDetail.remove')}
                              </Button>
                            </Space>
                          ),
                        },
                      ]}
                    />
                  </section>

                  <Modal
                    open={publicScopeBinding !== null}
                    title={t('agentDetail.configurePublicScope')}
                    okText={t('agentDetail.savePublicScope')}
                    confirmLoading={busy}
                    okButtonProps={{ disabled: publicScopeLoading }}
                    onOk={() => void savePublicScope()}
                    onCancel={() => setPublicScopeBinding(null)}
                  >
                    <Space direction="vertical" size={16} style={{ width: '100%' }}>
                      <Text type="secondary">
                        {t('agentDetail.publicScopeDescription')}
                      </Text>
                      <Radio.Group
                        aria-label={t('agentDetail.publicScope')}
                        value={publicScopeDraft === null ? 'all' : 'selected'}
                        onChange={(event) =>
                          setPublicScopeDraft(
                            event.target.value === 'all' ? null : [],
                          )
                        }
                      >
                        <Space direction="vertical">
                          <Radio value="all">
                            {t('agentDetail.allPublicResources')}
                          </Radio>
                          <Radio value="selected">
                            {t('agentDetail.selectedPublicResourceOption')}
                          </Radio>
                        </Space>
                      </Radio.Group>
                      <Select
                        mode="multiple"
                        aria-label={t('agentDetail.publicKnowledgeResources')}
                        loading={publicScopeLoading}
                        disabled={publicScopeDraft === null}
                        value={publicScopeDraft ?? []}
                        onChange={setPublicScopeDraft}
                        options={publicScopeOptions.map((resource) => ({
                          value: resource.knowledge_resource_id,
                          label: `${resource.name} · ${resource.knowledge_space_name}`,
                        }))}
                        style={{ width: '100%' }}
                      />
                    </Space>
                  </Modal>

                  <Divider style={{ margin: 0 }} />

                  <section aria-label={t('agentDetail.polarragAccessLabel')}>
                    <PolarRAGAccessPanel
                      agentId={agent.id}
                      bindings={polarRAGBindings}
                      onBindingsChange={setPolarRAGBindings}
                    />
                  </section>
                </Space>
              ),
            },
          ]}
        />
      </Space>

      <Modal
        title={
          confirmation?.kind === 'regenerate'
            ? t('agentDetail.regenerateTitle')
            : confirmation?.kind === 'revoke'
              ? t('agentDetail.revokeTitle')
              : confirmation?.kind === 'status'
                ? t('agents.statusTitle', { action: agent.status === 'active' ? t('agents.disable') : t('agents.enable') })
                : t('agentDetail.removeAccessTitle')
        }
        open={confirmation !== null}
        okText={
          confirmation?.kind === 'regenerate'
            ? t('agentDetail.confirmRegenerate')
            : confirmation?.kind === 'revoke'
              ? t('agentDetail.confirmRevoke')
              : confirmation?.kind === 'status'
                ? `Confirm ${agent.status === 'active' ? 'disable' : 'enable'}`
                : t('agentDetail.confirmRemove')
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
              message={t('agentDetail.tokenInvalidWarning')}
              description={t('agentDetail.tokenUpdateDescription')}
            />
          )}
          {confirmation?.kind === 'revoke' && (
            <Alert
              type="warning"
              showIcon
              message={t('agentDetail.tokenRevokedWarning')}
            />
          )}
          {confirmation?.kind === 'status' && (
            <Text>
              {t('agentDetail.statusChangeDescription')}
            </Text>
          )}
          {confirmation?.kind === 'delete-access' && (
            <Text>
              {t('agentDetail.accessRemoveDescription')}
            </Text>
          )}
          {confirmation?.kind === 'delete-access' &&
            accessDeleteConflict?.instanceId ===
              confirmation.access.instance_id && (
              <Alert
                type="warning"
                showIcon
                role="alert"
                message={t('agentDetail.activeResourcesWarning')}
                description={t('agentDetail.resourceConflict', { count: accessDeleteConflict.resourceCount })}
                action={
                  <Button
                    size="small"
                    loading={busy}
                    onClick={() =>
                      void disableManagedDatabaseCreation()
                    }
                  >
                    {t('agentDetail.disableCreation')}
                  </Button>
                }
              />
            )}
        </Space>
      </Modal>

    </PageContainer>
  )
}
