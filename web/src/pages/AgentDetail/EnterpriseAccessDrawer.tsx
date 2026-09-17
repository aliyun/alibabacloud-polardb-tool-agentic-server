import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
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
import type { PolarRAGInstance } from '../../api/polarrag'

const { Text } = Typography
const DIRECTORY_PAGE_SIZE = 100
const DIRECTORY_SEARCH_DEBOUNCE_MS = 300

type DirectoryEntryType = 'users' | 'groups'

interface DirectoryPage {
  offset: number
  search: string
  total: number
}

const initialDirectoryPages: Record<DirectoryEntryType, DirectoryPage> = {
  groups: { offset: 0, search: '', total: 0 },
  users: { offset: 0, search: '', total: 0 },
}

interface StalePreviewErrorBody {
  detail?: {
    code?: string
    message?: string
    preview?: EnterpriseAccessPreview
  }
}

async function listDirectoryCandidates(
  sourceId: string,
  entryType: DirectoryEntryType,
  options: { offset?: number; search?: string } = {},
) {
  const response = await listEnterpriseIdentitySourceDirectory(sourceId, entryType, {
    offset: options.offset ?? 0,
    limit: DIRECTORY_PAGE_SIZE,
    ...(options.search ? { search: options.search } : {}),
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
  instances = [],
  open,
  onClose,
  onApplied,
}: {
  agentId: string
  bindings: AgentPolarRAGBinding[]
  instances?: PolarRAGInstance[]
  open: boolean
  onClose: () => void
  onApplied: () => void | Promise<void>
}) {
  const { t } = useTranslation()
  const [sources, setSources] = useState<EnterpriseIdentitySourceCandidate[]>([])
  const [spaces, setSpaces] = useState<EnterpriseSpaceCandidate[]>([])
  const [groups, setGroups] = useState<EnterpriseDirectoryGroupCandidate[]>([])
  const [users, setUsers] = useState<EnterpriseDirectoryUserCandidate[]>([])
  const [selectedGroups, setSelectedGroups] = useState<
    EnterpriseDirectoryGroupCandidate[]
  >([])
  const [selectedUsers, setSelectedUsers] = useState<
    (EnterpriseDirectoryUserCandidate & { pas_user_id: string })[]
  >([])
  const [directoryPages, setDirectoryPages] = useState<
    Record<DirectoryEntryType, DirectoryPage>
  >(initialDirectoryPages)
  const [identitySourceId, setIdentitySourceId] = useState<string>()
  const [allSyncedUsers, setAllSyncedUsers] = useState(false)
  const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([])
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([])
  const [selectedInstanceIds, setSelectedInstanceIds] = useState<string[]>([])
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
    setSelectedInstanceIds([])
    setSelectedGroups([])
    setSelectedUsers([])
    setSelectedSpaceIds([])
    setPreview(null)
    setStalePreview(false)
  }, [])

  const changeSelectedGroupIds = useCallback(
    (groupIds: string[]) => {
      selectedGroupIdsRef.current = groupIds
      setSelectedGroupIds(groupIds)
      setSelectedGroups((current) =>
        groupIds.flatMap((groupId) => {
          const group =
            groups.find((candidate) => candidate.id === groupId) ??
            current.find((candidate) => candidate.id === groupId)
          return group ? [group] : []
        }),
      )
    },
    [groups],
  )

  const changeSelectedUserIds = useCallback(
    (userIds: string[]) => {
      selectedUserIdsRef.current = userIds
      setSelectedUserIds(userIds)
      setSelectedUsers((current) =>
        userIds.flatMap((userId) => {
          const user =
            users
              .filter(isMappedActiveUser)
              .find((candidate) => candidate.pas_user_id === userId) ??
            current.find((candidate) => candidate.pas_user_id === userId)
          return user ? [user] : []
        }),
      )
    },
    [users],
  )

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
    setDirectoryPages(initialDirectoryPages)
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
          sourceResponse.data.items.filter((source) => source.status !== 'disabled'),
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
  const activeInstances = useMemo(
    () =>
      [...instances]
        .filter((instance) => instance.status === 'active')
        .sort((left, right) => {
          const bindingDifference =
            Number(boundInstanceIds.has(right.id)) -
            Number(boundInstanceIds.has(left.id))
          return bindingDifference || left.name.localeCompare(right.name)
        }),
    [boundInstanceIds, instances],
  )
  const selectedInstanceIdSet = useMemo(
    () => new Set(selectedInstanceIds),
    [selectedInstanceIds],
  )
  const eligibleSpaces = useMemo(
    () =>
      spaces.filter((space) =>
        selectedInstanceIdSet.has(space.polarrag_instance_id),
      ),
    [selectedInstanceIdSet, spaces],
  )
  const allEligibleSpacesSelected =
    eligibleSpaces.length > 0 &&
    eligibleSpaces.every((space) =>
      selectedSpaceIds.includes(space.knowledge_space_id),
    )
  const mappedUsers = useMemo(
    () =>
      mergeCandidates(
        selectedUsers,
        users.filter(isMappedActiveUser),
        (user) => user.pas_user_id,
      ),
    [selectedUsers, users],
  )
  const availableGroups = useMemo(
    () =>
      mergeCandidates(
        selectedGroups,
        groups.filter(
          (group) =>
            group.status === 'active' &&
            (group.principal_type === 'group' ||
              group.principal_type === 'department'),
        ),
        (group) => group.id,
      ),
    [groups, selectedGroups],
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
    setDirectoryPages(initialDirectoryPages)
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
            group.status === 'active' &&
            (group.principal_type === 'group' || group.principal_type === 'department'),
        ),
      )
      setUsers(userResponse.users)
      setDirectoryPages({
        groups: {
          offset: 0,
          search: '',
          total: groupResponse.total ?? 0,
        },
        users: {
          offset: 0,
          search: '',
          total: userResponse.total ?? 0,
        },
      })
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

  const loadDirectoryPage = useCallback(
    (
      entryType: DirectoryEntryType,
      offset: number,
      search: string,
      requestId = directorySearchRequestIds.current[entryType] + 1,
    ) => {
      if (!identitySourceId) return
      directorySearchRequestIds.current[entryType] = requestId
      const directoryGeneration = directoryRequestId.current
      setDirectoryLoading(true)
      void listDirectoryCandidates(identitySourceId, entryType, { offset, search })
        .then((response) => {
          if (
            directoryGeneration !== directoryRequestId.current ||
            requestId !== directorySearchRequestIds.current[entryType]
          ) {
            return
          }
          setDirectoryPages((current) => ({
            ...current,
            [entryType]: { offset, search, total: response.total ?? 0 },
          }))
          if (entryType === 'groups') {
            setGroups(
              response.groups.filter(
                (group) =>
                  group.status === 'active' &&
                  (group.principal_type === 'group' ||
                    group.principal_type === 'department'),
              ),
            )
            return
          }
          setUsers(response.users)
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
    },
    [identitySourceId, t],
  )

  const searchDirectory = useCallback(
    (entryType: DirectoryEntryType, search: string) => {
      if (!identitySourceId) return
      const existingTimer = directorySearchTimers.current[entryType]
      if (existingTimer) clearTimeout(existingTimer)

      const requestId = directorySearchRequestIds.current[entryType] + 1
      directorySearchRequestIds.current[entryType] = requestId
      setDirectoryLoading(true)
      directorySearchTimers.current[entryType] = setTimeout(() => {
        loadDirectoryPage(entryType, 0, search, requestId)
      }, DIRECTORY_SEARCH_DEBOUNCE_MS)
    },
    [identitySourceId, loadDirectoryPage],
  )

  const hasSubject =
    allSyncedUsers || selectedGroupIds.length > 0 || selectedUserIds.length > 0
  const selectedSource = sources.find((source) => source.id === identitySourceId)
  const sourceHasSnapshot =
    selectedSource?.last_synced_at !== null &&
    (selectedSource?.status === 'active' || selectedSource?.status === 'stale')
  const selectedSpaceInstanceIds = new Set(
    eligibleSpaces
      .filter((space) =>
        selectedSpaceIds.includes(space.knowledge_space_id),
      )
      .map((space) => space.polarrag_instance_id),
  )
  const everySelectedInstanceHasSpace = selectedInstanceIds.every(
    (instanceId) => selectedSpaceInstanceIds.has(instanceId),
  )
  const canPreview =
    sourceHasSnapshot &&
    hasSubject &&
    selectedInstanceIds.length > 0 &&
    selectedSpaceIds.length > 0 &&
    everySelectedInstanceHasSpace

  const renderDirectoryPopup = (
    entryType: DirectoryEntryType,
    menu: ReactNode,
  ) => {
    const page = directoryPages[entryType]
    const pageCount = Math.max(1, Math.ceil(page.total / DIRECTORY_PAGE_SIZE))
    const currentPage = Math.floor(page.offset / DIRECTORY_PAGE_SIZE) + 1
    const isGroup = entryType === 'groups'
    const previousLabel = t(
      isGroup
        ? 'enterpriseAccess.previousEnterpriseGroupPage'
        : 'enterpriseAccess.previousSynchronizedUserPage',
    )
    const nextLabel = t(
      isGroup
        ? 'enterpriseAccess.nextEnterpriseGroupPage'
        : 'enterpriseAccess.nextSynchronizedUserPage',
    )
    return (
      <>
        {menu}
        {pageCount > 1 && (
          <Space
            align="center"
            style={{ display: 'flex', justifyContent: 'center', padding: 8 }}
            onMouseDown={(event) => {
              event.preventDefault()
              event.stopPropagation()
            }}
          >
            <Button
              aria-label={previousLabel}
              disabled={currentPage === 1}
              size="small"
              type="text"
              onClick={() =>
                loadDirectoryPage(
                  entryType,
                  Math.max(0, page.offset - DIRECTORY_PAGE_SIZE),
                  page.search,
                )
              }
            >
              ‹
            </Button>
            <Text type="secondary">
              {currentPage} / {pageCount}
            </Text>
            <Button
              aria-label={nextLabel}
              disabled={currentPage === pageCount}
              size="small"
              type="text"
              onClick={() =>
                loadDirectoryPage(
                  entryType,
                  Math.min(
                    page.offset + DIRECTORY_PAGE_SIZE,
                    (pageCount - 1) * DIRECTORY_PAGE_SIZE,
                  ),
                  page.search,
                )
              }
            >
              ›
            </Button>
          </Space>
        )}
      </>
    )
  }

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
        polarrag_instance_ids: selectedInstanceIds,
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
            label: `${source.name} (${source.status})`,
          }))}
          onChange={(sourceId) => void chooseSource(sourceId)}
          style={{ width: '100%' }}
        />

        {selectedSource && !sourceHasSnapshot && (
          <Alert
            type="warning"
            showIcon
            message={t('enterpriseAccess.sourceNotReady')}
          />
        )}

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
                  options={availableGroups.map((group) => ({
                    value: group.id,
                    label:
                      group.principal_type === 'department'
                        ? `${group.display_name} (department)`
                        : group.display_name,
                  }))}
                  onChange={changeSelectedGroupIds}
                  showSearch
                  filterOption={false}
                  loading={directoryLoading}
                  onSearch={(search) => searchDirectory('groups', search)}
                  popupRender={(menu) => renderDirectoryPopup('groups', menu)}
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
                  popupRender={(menu) => renderDirectoryPopup('users', menu)}
                  style={{ width: '100%' }}
                />
              </>
            )}
            <Select
              aria-label={t('enterpriseAccess.instances')}
              mode="multiple"
              value={selectedInstanceIds}
              placeholder={t('enterpriseAccess.selectInstances')}
              options={activeInstances.map((instance) => ({
                value: instance.id,
                label: instance.name,
              }))}
              onChange={(instanceIds) => {
                const nextInstanceIds = new Set(instanceIds)
                setSelectedInstanceIds(instanceIds)
                setSelectedSpaceIds((current) =>
                  current.filter((spaceId) => {
                    const space = spaces.find(
                      (candidate) =>
                        candidate.knowledge_space_id === spaceId,
                    )
                    return Boolean(
                      space &&
                        nextInstanceIds.has(space.polarrag_instance_id),
                    )
                  }),
                )
              }}
              showSearch
              optionFilterProp="label"
              style={{ width: '100%' }}
            />
            <Select
              aria-label={t('enterpriseAccess.spaces')}
              mode="multiple"
              value={selectedSpaceIds}
              placeholder={t('enterpriseAccess.selectSpaces')}
              disabled={selectedInstanceIds.length === 0}
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
