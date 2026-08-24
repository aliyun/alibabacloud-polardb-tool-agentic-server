import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  List,
  Select,
  Space,
  Typography,
} from 'antd'
import { useTranslation } from 'react-i18next'

import type { AgentPolarRAGBinding } from '../../api/agents'
import {
  applyAgentEnterpriseAccess,
  listEnterpriseIdentitySourceDirectory,
  listEnterpriseIdentitySources,
  listEnterpriseIdentitySourceSpaces,
  previewAgentEnterpriseAccess,
  type EnterpriseAccessImpact,
  type EnterpriseAccessPreview,
  type EnterpriseDirectoryGroupCandidate,
  type EnterpriseDirectoryUserCandidate,
  type EnterpriseIdentitySourceCandidate,
  type EnterpriseSpaceCandidate,
} from '../../api/enterpriseAccess'
import { getAPIErrorMessage } from '../../api/client'

const { Text } = Typography
const DIRECTORY_PAGE_SIZE = 100
const DIRECTORY_SEARCH_DEBOUNCE_MS = 300

interface StalePreviewErrorBody {
  detail?: {
    code?: string
    message?: string
    preview?: EnterpriseAccessPreview
  }
}

async function listDirectoryCandidates(
  sourceId: string,
  entryType: 'users' | 'groups',
  search?: string,
) {
  const response = await listEnterpriseIdentitySourceDirectory(sourceId, entryType, {
    offset: 0,
    limit: DIRECTORY_PAGE_SIZE,
    ...(search ? { search } : {}),
  })
  return response.data
}

function mergeCandidates<T extends { id: string }>(
  selected: T[],
  candidates: T[],
  key: (candidate: T) => string,
) {
  const byId = new Map(selected.map((candidate) => [key(candidate), candidate]))
  for (const candidate of candidates) {
    if (!byId.has(key(candidate))) {
      byId.set(key(candidate), candidate)
    }
  }
  return [...byId.values()]
}

function isMappedActiveUser(
  user: EnterpriseDirectoryUserCandidate,
): user is EnterpriseDirectoryUserCandidate & { pas_user_id: string } {
  return user.status === 'active' && user.pas_user_id !== null
}

function ImpactList({
  items,
  emptyText,
}: {
  items: EnterpriseAccessImpact[]
  emptyText: string
}) {
  if (items.length === 0) {
    return <Text type="secondary">{emptyText}</Text>
  }
  return (
    <List
      size="small"
      dataSource={items}
      renderItem={(item) => <List.Item>{item.display_name}</List.Item>}
    />
  )
}

export default function EnterpriseAccessDrawer({
  agentId,
  bindings,
  open,
  onClose,
  onApplied,
}: {
  agentId: string
  bindings: AgentPolarRAGBinding[]
  open: boolean
  onClose: () => void
  onApplied: () => void | Promise<void>
}) {
  const { t } = useTranslation()
  const [sources, setSources] = useState<EnterpriseIdentitySourceCandidate[]>([])
  const [spaces, setSpaces] = useState<EnterpriseSpaceCandidate[]>([])
  const [groups, setGroups] = useState<EnterpriseDirectoryGroupCandidate[]>([])
  const [users, setUsers] = useState<EnterpriseDirectoryUserCandidate[]>([])
  const [identitySourceId, setIdentitySourceId] = useState<string>()
  const [allSyncedUsers, setAllSyncedUsers] = useState(false)
  const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([])
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([])
  const [selectedSpaceIds, setSelectedSpaceIds] = useState<string[]>([])
  const [preview, setPreview] = useState<EnterpriseAccessPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stalePreview, setStalePreview] = useState(false)
  const [directoryLoading, setDirectoryLoading] = useState(false)
  const selectedGroupIdsRef = useRef<string[]>([])
  const selectedUserIdsRef = useRef<string[]>([])
  const directoryRequestId = useRef(0)
  const directorySearchRequestIds = useRef({ groups: 0, users: 0 })
  const directorySearchTimers = useRef<
    Partial<Record<'groups' | 'users', ReturnType<typeof setTimeout>>>
  >({})

  const resetSelections = useCallback(() => {
    setAllSyncedUsers(false)
    selectedGroupIdsRef.current = []
    selectedUserIdsRef.current = []
    setSelectedGroupIds([])
    setSelectedUserIds([])
    setSelectedSpaceIds([])
    setPreview(null)
    setStalePreview(false)
  }, [])

  const changeSelectedGroupIds = useCallback((groupIds: string[]) => {
    selectedGroupIdsRef.current = groupIds
    setSelectedGroupIds(groupIds)
  }, [])

  const changeSelectedUserIds = useCallback((userIds: string[]) => {
    selectedUserIdsRef.current = userIds
    setSelectedUserIds(userIds)
  }, [])

  const invalidateDirectoryRequests = useCallback(() => {
    directoryRequestId.current += 1
    directorySearchRequestIds.current.groups += 1
    directorySearchRequestIds.current.users += 1
    for (const timer of Object.values(directorySearchTimers.current)) {
      clearTimeout(timer)
    }
    directorySearchTimers.current = {}
    setDirectoryLoading(false)
  }, [])

  const resetDrawer = useCallback(() => {
    invalidateDirectoryRequests()
    setIdentitySourceId(undefined)
    setGroups([])
    setUsers([])
    setError(null)
    resetSelections()
  }, [invalidateDirectoryRequests, resetSelections])

  useEffect(() => {
    if (!open) {
      resetDrawer()
      return
    }
    resetDrawer()
    setLoading(true)
    void Promise.all([
      listEnterpriseIdentitySources(),
      listEnterpriseIdentitySourceSpaces(),
    ])
      .then(([sourceResponse, spaceResponse]) => {
        setSources(
          sourceResponse.data.items.filter((source) => source.status === 'active'),
        )
        setSpaces(spaceResponse.data.items)
      })
      .catch((requestError: unknown) => {
        setError(
          getAPIErrorMessage(
            requestError,
            t('enterpriseAccess.loadFailed'),
          ),
        )
      })
      .finally(() => setLoading(false))
  }, [open, resetDrawer, t])

  const boundInstanceIds = useMemo(
    () => new Set(bindings.map((binding) => binding.polarrag_instance_id)),
    [bindings],
  )
  const eligibleSpaces = useMemo(
    () =>
      spaces.filter((space) =>
        boundInstanceIds.has(space.polarrag_instance_id),
      ),
    [boundInstanceIds, spaces],
  )
  const allEligibleSpacesSelected =
    eligibleSpaces.length > 0 &&
    eligibleSpaces.every((space) =>
      selectedSpaceIds.includes(space.knowledge_space_id),
    )
  const mappedUsers = useMemo(
    () =>
      mergeCandidates(
        [],
        users.filter(isMappedActiveUser),
        (user) => user.pas_user_id,
      ),
    [users],
  )

  const chooseSource = async (sourceId: string) => {
    const requestId = directoryRequestId.current + 1
    directoryRequestId.current = requestId
    directorySearchRequestIds.current.groups += 1
    directorySearchRequestIds.current.users += 1
    for (const timer of Object.values(directorySearchTimers.current)) {
      clearTimeout(timer)
    }
    directorySearchTimers.current = {}
    setIdentitySourceId(sourceId)
    setGroups([])
    setUsers([])
    setError(null)
    resetSelections()
    setDirectoryLoading(true)
    try {
      const [groupResponse, userResponse] = await Promise.all([
        listDirectoryCandidates(sourceId, 'groups'),
        listDirectoryCandidates(sourceId, 'users'),
      ])
      if (requestId !== directoryRequestId.current) return
      setGroups(
        groupResponse.groups.filter(
          (group) =>
            group.status === 'active' && group.principal_type === 'group',
        ),
      )
      setUsers(userResponse.users)
    } catch (requestError: unknown) {
      if (requestId !== directoryRequestId.current) return
      setError(
        getAPIErrorMessage(
          requestError,
          t('enterpriseAccess.directoryLoadFailed'),
        ),
      )
    } finally {
      if (requestId === directoryRequestId.current) {
        setDirectoryLoading(false)
      }
    }
  }

  const searchDirectory = useCallback(
    (entryType: 'groups' | 'users', search: string) => {
      if (!identitySourceId) return
      const existingTimer = directorySearchTimers.current[entryType]
      if (existingTimer) clearTimeout(existingTimer)

      const requestId = directorySearchRequestIds.current[entryType] + 1
      directorySearchRequestIds.current[entryType] = requestId
      const directoryGeneration = directoryRequestId.current
      setDirectoryLoading(true)
      directorySearchTimers.current[entryType] = setTimeout(() => {
        void listDirectoryCandidates(identitySourceId, entryType, search)
          .then((response) => {
            if (
              directoryGeneration !== directoryRequestId.current ||
              requestId !== directorySearchRequestIds.current[entryType]
            ) {
              return
            }
            if (entryType === 'groups') {
              const activeGroups = response.groups.filter(
                (group) =>
                  group.status === 'active' && group.principal_type === 'group',
              )
              setGroups((current) =>
                mergeCandidates(
                  current.filter((group) =>
                    selectedGroupIdsRef.current.includes(group.id),
                  ),
                  activeGroups,
                  (group) => group.id,
                ),
              )
              return
            }
            setUsers((current) =>
              mergeCandidates(
                current
                  .filter(isMappedActiveUser)
                  .filter((user) =>
                    selectedUserIdsRef.current.includes(user.pas_user_id),
                  ),
                response.users.filter(isMappedActiveUser),
                (user) => user.pas_user_id,
              ),
            )
          })
          .catch((requestError: unknown) => {
            if (
              directoryGeneration === directoryRequestId.current &&
              requestId === directorySearchRequestIds.current[entryType]
            ) {
              setError(
                getAPIErrorMessage(
                  requestError,
                  t('enterpriseAccess.directoryLoadFailed'),
                ),
              )
            }
          })
          .finally(() => {
            if (
              directoryGeneration === directoryRequestId.current &&
              requestId === directorySearchRequestIds.current[entryType]
            ) {
              setDirectoryLoading(false)
            }
          })
      }, DIRECTORY_SEARCH_DEBOUNCE_MS)
    },
    [identitySourceId, t],
  )

  const hasSubject =
    allSyncedUsers || selectedGroupIds.length > 0 || selectedUserIds.length > 0
  const canPreview =
    Boolean(identitySourceId) && hasSubject && selectedSpaceIds.length > 0

  const requestPreview = async () => {
    if (!identitySourceId || !canPreview) return
    setBusy(true)
    setError(null)
    setStalePreview(false)
    try {
      const response = await previewAgentEnterpriseAccess(agentId, {
        identity_source_id: identitySourceId,
        all_synced_users: allSyncedUsers,
        directory_group_ids: selectedGroupIds,
        pas_user_ids: selectedUserIds,
        knowledge_space_ids: selectedSpaceIds,
      })
      setPreview(response.data)
    } catch (requestError: unknown) {
      setError(
        getAPIErrorMessage(
          requestError,
          t('enterpriseAccess.previewFailed'),
        ),
      )
    } finally {
      setBusy(false)
    }
  }

  const applyPreview = async () => {
    if (!preview) return
    setBusy(true)
    setError(null)
    setStalePreview(false)
    try {
      await applyAgentEnterpriseAccess(agentId, {
        ...preview.selection,
        preview_hash: preview.preview_hash,
      })
      await onApplied()
      resetDrawer()
      onClose()
    } catch (requestError: unknown) {
      if (axios.isAxiosError<StalePreviewErrorBody>(requestError)) {
        const detail = requestError.response?.data?.detail
        if (
          requestError.response?.status === 409 &&
          detail?.code === 'ENTERPRISE_ACCESS_PREVIEW_STALE' &&
          detail.preview
        ) {
          setPreview(detail.preview)
          setStalePreview(true)
          return
        }
      }
      setError(
        getAPIErrorMessage(
          requestError,
          t('enterpriseAccess.applyFailed'),
        ),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Drawer
      title={t('enterpriseAccess.configure')}
      open={open}
      width={640}
      destroyOnHidden
      loading={loading}
      onClose={() => {
        resetDrawer()
        onClose()
      }}
      footer={
        <Space style={{ width: '100%', justifyContent: 'flex-end' }}>
          <Button
            onClick={() => {
              resetDrawer()
              onClose()
            }}
          >
            {t('enterpriseAccess.cancel')}
          </Button>
          {preview ? (
            <Button type="primary" loading={busy} onClick={() => void applyPreview()}>
              {t('enterpriseAccess.confirmAndConfigure')}
            </Button>
          ) : (
            <Button
              type="primary"
              disabled={!canPreview}
              loading={busy}
              onClick={() => void requestPreview()}
            >
              {t('enterpriseAccess.previewChanges')}
            </Button>
          )}
        </Space>
      }
    >
      <Space direction="vertical" size={20} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} />}
        {stalePreview && (
          <Alert
            type="warning"
            showIcon
            message={t('enterpriseAccess.previewStale')}
          />
        )}
        <Select
          aria-label={t('enterpriseAccess.identitySource')}
          value={identitySourceId}
          placeholder={t('enterpriseAccess.selectIdentitySource')}
          options={sources.map((source) => ({
            value: source.id,
            label: source.name,
          }))}
          onChange={(sourceId) => void chooseSource(sourceId)}
          style={{ width: '100%' }}
        />

        {identitySourceId && !preview && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Checkbox
              aria-label={t('enterpriseAccess.allSynchronizedUsers')}
              checked={allSyncedUsers}
              onChange={(event) => {
                setAllSyncedUsers(event.target.checked)
                setPreview(null)
              }}
            >
              {t('enterpriseAccess.allSynchronizedUsers')}
            </Checkbox>
            {!allSyncedUsers && (
              <>
                <Select
                  aria-label={t('enterpriseAccess.enterpriseGroups')}
                  mode="multiple"
                  value={selectedGroupIds}
                  placeholder={t('enterpriseAccess.selectEnterpriseGroups')}
                  options={groups.map((group) => ({
                    value: group.id,
                    label: group.display_name,
                  }))}
                  onChange={changeSelectedGroupIds}
                  showSearch
                  filterOption={false}
                  loading={directoryLoading}
                  onSearch={(search) => searchDirectory('groups', search)}
                  style={{ width: '100%' }}
                />
                <Select
                  aria-label={t('enterpriseAccess.synchronizedUsers')}
                  mode="multiple"
                  value={selectedUserIds}
                  placeholder={t('enterpriseAccess.selectSynchronizedUsers')}
                  options={mappedUsers.map((user) => ({
                    value: user.pas_user_id,
                    label: user.display_name,
                  }))}
                  onChange={changeSelectedUserIds}
                  showSearch
                  filterOption={false}
                  loading={directoryLoading}
                  onSearch={(search) => searchDirectory('users', search)}
                  style={{ width: '100%' }}
                />
              </>
            )}
            <Select
              aria-label={t('enterpriseAccess.spaces')}
              mode="multiple"
              value={selectedSpaceIds}
              placeholder={t('enterpriseAccess.selectSpaces')}
              options={eligibleSpaces.map((space) => ({
                value: space.knowledge_space_id,
                label: space.name,
              }))}
              onChange={setSelectedSpaceIds}
              popupRender={(menu) => (
                <>
                  <Button
                    block
                    type="text"
                    onMouseDown={(event) => {
                      event.preventDefault()
                      event.stopPropagation()
                    }}
                    onClick={() => {
                      setSelectedSpaceIds(
                        allEligibleSpacesSelected
                          ? []
                          : eligibleSpaces.map(
                              (space) => space.knowledge_space_id,
                            ),
                      )
                    }}
                  >
                    {t(
                      allEligibleSpacesSelected
                        ? 'enterpriseAccess.clearAllSpaces'
                        : 'enterpriseAccess.selectAllSpaces',
                    )}
                  </Button>
                  {menu}
                </>
              )}
              showSearch
              optionFilterProp="label"
              style={{ width: '100%' }}
            />
          </Space>
        )}

        {preview && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Card size="small" title={t('enterpriseAccess.agentChanges')}>
              <ImpactList
                items={preview.creates}
                emptyText={t('enterpriseAccess.noChanges')}
              />
            </Card>
            <Card size="small" title={t('enterpriseAccess.globalChanges')}>
              {preview.global_changes.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message={t('enterpriseAccess.globalChangesWarning')}
                  style={{ marginBottom: 12 }}
                />
              )}
              <ImpactList
                items={preview.global_changes}
                emptyText={t('enterpriseAccess.noChanges')}
              />
            </Card>
            <Card size="small" title={t('enterpriseAccess.alreadyConfigured')}>
              <ImpactList
                items={preview.reuses}
                emptyText={t('enterpriseAccess.noChanges')}
              />
            </Card>
          </Space>
        )}
      </Space>
    </Drawer>
  )
}
