import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  claimPolarRAGKnowledgeBase,
  disablePolarRAGSpace,
  enablePolarRAGSpace,
  listPolarRAGInstances,
  listPolarRAGSpaces,
  listUnclaimedPolarRAGKnowledgeBases,
  syncPolarRAGSpace,
  configurePolarRAGSpaceOss,
  updatePolarRAGInstance,
} from '../../api/polarrag'
import InstancesPanel from './InstancesPanel'

vi.mock('../../api/polarrag', () => ({
  claimPolarRAGKnowledgeBase: vi.fn(),
  checkPolarRAGInstance: vi.fn(),
  disablePolarRAGInstance: vi.fn(),
  disablePolarRAGSpace: vi.fn(),
  enablePolarRAGSpace: vi.fn(),
  listPolarRAGInstances: vi.fn(),
  listPolarRAGSpaces: vi.fn(),
  listUnclaimedPolarRAGKnowledgeBases: vi.fn(),
  syncPolarRAGSpace: vi.fn(),
  configurePolarRAGSpaceOss: vi.fn(),
  updatePolarRAGInstance: vi.fn(),
}))

const instance = {
  id: 'rag-1',
  name: 'Primary RAG',
  scheme: 'https' as const,
  host: 'rag.example.test',
  port: 9200,
  tls_verify: true,
  status: 'capability_missing' as const,
  plugin_version: '3.2.0',
  capabilities: {
    version: '3.2.0',
    search: true,
    protected_document_info: true,
    protected_context: true,
    protected_document_search: true,
    space_catalog: false,
    knowledge_base_catalog: false,
  },
  last_checked_at: '2026-07-30T12:00:00Z',
  last_error_code: 'POLARRAG_CATALOG_CAPABILITY_MISSING',
  created_at: '2026-07-30T12:00:00Z',
  updated_at: null,
}

describe('PolarRAG instance inventory', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listPolarRAGInstances).mockResolvedValue({
      data: { items: [instance] },
    } as never)
    vi.mocked(listUnclaimedPolarRAGKnowledgeBases).mockResolvedValue({
      data: { items: [], owner_candidates: [] },
    } as never)
  })

  it('shows capability gaps without a second registration entry', async () => {
    render(<InstancesPanel />)

    expect(
      await screen.findByText('POLARRAG_CATALOG_CAPABILITY_MISSING'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/trusted Space and KB catalog APIs/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /register instance/i }),
    ).not.toBeInTheDocument()
  })

  it('keeps every instance column in the same horizontal scroll layer', async () => {
    render(<InstancesPanel />)

    await screen.findByText('Primary RAG')
    const table = screen.getByRole('table')
    expect(table.querySelector('.ant-table-cell-fix-left')).toBeNull()
    expect(table.querySelector('.ant-table-cell-fix-right')).toBeNull()
  })

  it('matches the database empty state and delegates registration', async () => {
    const user = userEvent.setup()
    const onRegister = vi.fn()
    vi.mocked(listPolarRAGInstances).mockResolvedValue({
      data: { items: [] },
    } as never)

    render(<InstancesPanel onRegister={onRegister} />)

    expect(
      await screen.findByText('No PolarRAG instances registered'),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        'Register and verify an instance before enabling Spaces.',
      ),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/trusted retrieval starts/i),
    ).not.toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: /register instance/i }),
    )
    expect(onRegister).toHaveBeenCalledOnce()
  })

  it('enumerates upstream Spaces and keeps identity_domain read-only', async () => {
    const user = userEvent.setup()
    const disabledSpace = {
      space_id: 'space-a',
      name: 'Engineering',
      identity_domain: 'tenant-a',
      status: 'ACTIVE',
      enabled: false,
      knowledge_space_id: null,
      last_synced_at: null,
      knowledge_resources: [],
    }
    const enabledSpace = {
      ...disabledSpace,
      enabled: true,
      knowledge_space_id: 'opaque-space',
      last_synced_at: '2026-07-31T00:00:00Z',
      knowledge_resources: [
        {
          knowledge_resource_id: 'opaque-resource',
          name: 'Public KB',
          kb_type: 'PUBLIC',
          binding_mode: 'domain',
          sync_status: 'active',
          enabled: true,
        },
      ],
    }
    vi.mocked(listPolarRAGSpaces).mockResolvedValueOnce({
      data: {
        items: [disabledSpace],
      },
    } as never)
    vi.mocked(listPolarRAGSpaces).mockResolvedValue({
      data: { items: [enabledSpace] },
    } as never)
    vi.mocked(enablePolarRAGSpace).mockResolvedValue({
      data: {
        knowledge_space_id: 'opaque-space',
        name: 'Engineering',
        identity_domain: 'tenant-a',
        enabled: true,
        sync: { active: 2, disabled: 0, owner_unresolved: 0 },
      },
    } as never)
    vi.mocked(syncPolarRAGSpace).mockResolvedValue({
      data: { active: 2, disabled: 0, owner_unresolved: 0 },
    } as never)
    render(<InstancesPanel />)

    await user.click(
      await screen.findByRole('button', {
        name: /manage spaces for Primary RAG/i,
      }),
    )
    expect(await screen.findByText('Engineering')).toBeInTheDocument()
    expect(screen.getByText('tenant-a')).toBeInTheDocument()
    expect(screen.queryByLabelText(/identity domain/i)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /enable Engineering/i }))
    await waitFor(() =>
      expect(enablePolarRAGSpace).toHaveBeenCalledWith('rag-1', 'space-a'),
    )
    expect(await screen.findByText('Public KB')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('opaque-resource')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /sync Engineering/i }))
    await waitFor(() =>
      expect(syncPolarRAGSpace).toHaveBeenCalledWith('rag-1', 'space-a'),
    )
    await user.click(
      screen.getByRole('button', { name: /disable Engineering/i }),
    )
    await user.click(
      await screen.findByRole('button', { name: /^disable$/i }),
    )
    await waitFor(() =>
      expect(disablePolarRAGSpace).toHaveBeenCalledWith(
        'rag-1',
        'space-a',
      ),
    )
  })

  it('lets an administrator assign an owner and activate an unclaimed KB', async () => {
    const user = userEvent.setup()
    const enabledSpace = {
      space_id: 'space-a',
      name: 'Engineering',
      identity_domain: 'tenant-a',
      status: 'ACTIVE',
      enabled: true,
      knowledge_space_id: 'opaque-space',
      last_synced_at: null,
      knowledge_resources: [],
    }
    vi.mocked(listPolarRAGSpaces).mockResolvedValue({
      data: { items: [enabledSpace] },
    } as never)
    vi.mocked(listUnclaimedPolarRAGKnowledgeBases)
      .mockResolvedValueOnce({
        data: {
          items: [
            {
              space_id: 'space-a',
              space_name: 'Engineering',
              identity_domain: 'tenant-a',
              kb_id: 'public-kb',
              name: 'Public KB',
              kb_type: 'PUBLIC',
              status: 'UNCLAIMED',
            },
          ],
          owner_candidates: [
            {
              principal_assignment_id: 'principal-1',
              pas_user_id: 'user-1',
              user_name: 'Allen',
              user_external_id: 'allen',
              identity_domain: 'tenant-a',
              provider: 'feishu',
              principal_id: '053317',
            },
          ],
        },
      } as never)
      .mockResolvedValue({
        data: { items: [], owner_candidates: [] },
      } as never)
    vi.mocked(claimPolarRAGKnowledgeBase).mockResolvedValue({
      data: {
        kb_id: 'public-kb',
        status: 'ACTIVE',
        sync: { active: 1, disabled: 0, owner_unresolved: 0 },
      },
    } as never)

    render(<InstancesPanel />)
    await user.click(
      await screen.findByRole('button', {
        name: /manage spaces for Primary RAG/i,
      }),
    )
    expect(await screen.findByText('Public KB')).toBeInTheDocument()
    await user.click(
      screen.getByRole('combobox', { name: /owner for Public KB/i }),
    )
    await user.click(
      await screen.findByText('Allen · allen · feishu'),
    )
    await user.click(
      screen.getByRole('button', {
        name: /assign owner and activate Public KB/i,
      }),
    )

    await waitFor(() =>
      expect(claimPolarRAGKnowledgeBase).toHaveBeenCalledWith(
        'rag-1',
        'space-a',
        'public-kb',
        { principal_assignment_id: 'principal-1' },
      ),
    )
    await waitFor(() =>
      expect(listUnclaimedPolarRAGKnowledgeBases).toHaveBeenCalledTimes(2),
    )
  })

  it('assigns an active PAS user through the native PolarRAG principal', async () => {
    const user = userEvent.setup()
    vi.mocked(listPolarRAGSpaces).mockResolvedValue({
      data: {
        items: [{
          space_id: 'space-a',
          name: 'Engineering',
          identity_domain: 'tenant-a',
          status: 'ACTIVE',
          enabled: true,
          knowledge_space_id: 'opaque-space',
          last_synced_at: null,
          knowledge_resources: [],
        }],
      },
    } as never)
    vi.mocked(listUnclaimedPolarRAGKnowledgeBases)
      .mockResolvedValueOnce({
        data: {
          items: [{
            space_id: 'space-a',
            space_name: 'Engineering',
            identity_domain: 'tenant-a',
            kb_id: 'personal-kb',
            name: 'Personal KB',
            kb_type: 'PERSONAL',
            status: 'UNCLAIMED',
          }],
          owner_candidates: [{
            principal_assignment_id: null,
            pas_user_id: 'user-1',
            user_name: 'Allen',
            user_external_id: 'allen',
            identity_domain: 'tenant-a',
            provider: 'polarrag',
            principal_id: 'allen',
          }],
        },
      } as never)
      .mockResolvedValue({
        data: { items: [], owner_candidates: [] },
      } as never)
    vi.mocked(claimPolarRAGKnowledgeBase).mockResolvedValue({
      data: {
        kb_id: 'personal-kb',
        status: 'ACTIVE',
        sync: { active: 1, disabled: 0, owner_unresolved: 0 },
      },
    } as never)

    render(<InstancesPanel />)
    await user.click(
      await screen.findByRole('button', {
        name: /manage spaces for Primary RAG/i,
      }),
    )
    await user.click(
      screen.getByRole('combobox', { name: /owner for Personal KB/i }),
    )
    await user.click(await screen.findByText('Allen · allen · polarrag'))
    await user.click(
      screen.getByRole('button', {
        name: /assign owner and activate Personal KB/i,
      }),
    )

    await waitFor(() =>
      expect(claimPolarRAGKnowledgeBase).toHaveBeenCalledWith(
        'rag-1',
        'space-a',
        'personal-kb',
        { pas_user_id: 'user-1' },
      ),
    )
  })

  it('paginates synchronized knowledge bases in the Spaces drawer', async () => {
    const user = userEvent.setup()
    vi.mocked(listPolarRAGSpaces).mockResolvedValue({
      data: {
        items: [
          {
            space_id: 'space-a',
            name: 'Engineering',
            identity_domain: 'tenant-a',
            status: 'ACTIVE',
            enabled: true,
            knowledge_space_id: 'opaque-space',
            last_synced_at: null,
            knowledge_resources: Array.from({ length: 11 }, (_, index) => ({
              knowledge_resource_id: `resource-${index + 1}`,
              name: `Knowledge base ${index + 1}`,
              kb_type: 'PUBLIC',
              binding_mode: 'domain',
              sync_status: 'active',
              enabled: true,
            })),
          },
        ],
      },
    } as never)

    render(<InstancesPanel />)
    await user.click(
      await screen.findByRole('button', {
        name: /manage spaces for Primary RAG/i,
      }),
    )

    expect(await screen.findByText('Knowledge base 1')).toBeInTheDocument()
    expect(screen.queryByText('Knowledge base 11')).not.toBeInTheDocument()
    await user.click(screen.getByTitle('2'))
    expect(await screen.findByText('Knowledge base 11')).toBeInTheDocument()
  })

  it('rotates instance credentials without revealing stored secrets', async () => {
    const user = userEvent.setup()
    vi.mocked(updatePolarRAGInstance).mockResolvedValue({
      data: instance,
    } as never)
    render(<InstancesPanel />)

    await user.click(
      await screen.findByRole('button', {
        name: /rotate credentials for Primary RAG/i,
      }),
    )
    const dialog = screen.getByRole('dialog', {
      name: /rotate credentials/i,
    })
    await user.type(within(dialog).getByLabelText(/^username$/i), 'pas-rotated')
    await user.type(
      within(dialog).getByLabelText(/^new password$/i),
      'rotated-secret',
    )
    await user.click(within(dialog).getByRole('button', { name: /^rotate$/i }))

    await waitFor(() =>
      expect(updatePolarRAGInstance).toHaveBeenCalledWith('rag-1', {
        username: 'pas-rotated',
        password: 'rotated-secret',
        tls_verify: true,
        ca_bundle: null,
      }),
    )
    expect(screen.queryByDisplayValue('rotated-secret')).not.toBeInTheDocument()
  })

  it('configures OSS against the read-only upstream location', async () => {
    const user = userEvent.setup()
    vi.mocked(listPolarRAGSpaces).mockResolvedValue({
      data: {
        items: [
          {
            space_id: 'space-a',
            name: 'Engineering',
            identity_domain: 'tenant-a',
            oss_bucket: 'tenant-a-documents',
            oss_endpoint: 'oss-cn-hangzhou.aliyuncs.com',
            status: 'ACTIVE',
            enabled: true,
            knowledge_space_id: 'opaque-space',
            last_synced_at: null,
            knowledge_resources: [],
          },
        ],
      },
    } as never)
    vi.mocked(configurePolarRAGSpaceOss).mockResolvedValue({
      data: {
        knowledge_space_id: 'opaque-space',
        bucket: 'tenant-a-documents',
        endpoint: 'oss-cn-hangzhou.aliyuncs.com',
        object_prefix: 'pas/documents',
        validated: true,
        validated_at: '2026-08-06T10:00:00Z',
      },
    } as never)

    render(<InstancesPanel />)
    await user.click(
      await screen.findByRole('button', {
        name: /manage spaces for Primary RAG/i,
      }),
    )
    const spacesDrawers = await screen.findAllByRole('dialog', {
      name: /spaces.*primary rag/i,
    })
    expect(spacesDrawers).toHaveLength(1)
    const [spacesDrawer] = spacesDrawers
    const configureButton = await within(spacesDrawer).findByRole('button', {
      name: /configure oss for Engineering/i,
    })
    expect(configureButton).toBeEnabled()
    await user.click(configureButton)
    const modalMessage = await screen.findByText(
      'OSS location is owned by PolarRAG',
    )
    const dialog = modalMessage.closest('[role="dialog"]')
    expect(dialog).toBeInstanceOf(HTMLElement)
    if (!(dialog instanceof HTMLElement)) throw new Error('Dialog not found')
    expect(within(dialog).getByLabelText(/^bucket$/i)).toHaveValue(
      'tenant-a-documents',
    )
    expect(within(dialog).getByLabelText(/^bucket$/i)).toHaveAttribute('readonly')
    expect(within(dialog).getByLabelText(/^endpoint$/i)).toHaveValue(
      'oss-cn-hangzhou.aliyuncs.com',
    )
    expect(within(dialog).getByLabelText(/^endpoint$/i)).toHaveAttribute(
      'readonly',
    )
    await user.type(within(dialog).getByLabelText(/^access key id$/i), 'ak-id')
    await user.type(
      within(dialog).getByLabelText(/^access key secret$/i),
      'ak-secret',
    )
    await user.click(within(dialog).getByRole('button', { name: /validate and save/i }))

    await waitFor(() =>
      expect(configurePolarRAGSpaceOss).toHaveBeenCalledWith('opaque-space', {
        access_key_id: 'ak-id',
        access_key_secret: 'ak-secret',
        object_prefix: 'pas/documents',
      }),
    )
  })
})
