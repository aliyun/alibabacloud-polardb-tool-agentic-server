import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  createPermissionTemplateRevision,
  listPermissionTemplates,
  requestPermissionSync,
} from '../../api/permissionTemplates'
import { updateDedicatedPool } from '../../api/dedicatedPools'
import PermissionTemplateDrawer from './PermissionTemplateDrawer'

vi.mock('../../api/permissionTemplates', () => ({
  createPermissionTemplateRevision: vi.fn(),
  listPermissionTemplates: vi.fn(),
  requestPermissionSync: vi.fn(),
}))

vi.mock('../../api/dedicatedPools', () => ({
  updateDedicatedPool: vi.fn(),
}))

describe('PermissionTemplateDrawer', () => {
  const templates = [
    {
      id: 'template-1',
      name: 'Agent default',
      description: null,
      created_at: '2026-08-10T00:00:00Z',
      revisions: [
        {
          id: 'revision-2',
          revision: 2,
          privileges: ['SELECT', 'INSERT'],
          grant_option: false,
          created_at: '2026-08-10T00:00:00Z',
        },
      ],
    },
  ]

  it('previews targets before an explicitly confirmed apply', async () => {
    vi.mocked(listPermissionTemplates).mockResolvedValue({
      data: templates,
    } as never)
    vi.mocked(requestPermissionSync)
      .mockResolvedValueOnce({
        data: {
          id: 'preview-1',
          template_revision_id: 'revision-2',
          target_scope: 'pool',
          target_id: 'pool-1',
          mode: 'dry_run',
          status: 'succeeded',
          total_count: 1,
          completed_count: 1,
          failed_count: 0,
          failure_reason: null,
          targets: [
            {
              id: 'target-1',
              member_id: 'member-1',
              resource_id: 'resource-1',
              previous_revision_id: 'revision-1',
              status: 'succeeded',
              change_required: true,
              retry_count: 0,
              failure_reason: null,
            },
          ],
        },
      } as never)
      .mockResolvedValueOnce({ data: { id: 'apply-1', status: 'pending' } } as never)

    const user = userEvent.setup()
    render(
      <PermissionTemplateDrawer
        open
        poolId="pool-1"
        configRevision={2}
        selectedRevisionId="revision-2"
        onClose={vi.fn()}
      />,
    )

    await user.click(
      await screen.findByRole('button', { name: /preview existing resources/i }),
    )
    expect(await screen.findByText(/1 account will change/i)).toBeInTheDocument()
    expect(requestPermissionSync).toHaveBeenNthCalledWith(1, 'revision-2', {
      target_scope: 'pool',
      target_id: 'pool-1',
      mode: 'dry_run',
      confirmed: false,
    })

    await user.click(screen.getByRole('button', { name: /apply to existing accounts/i }))
    await user.click(screen.getByRole('button', { name: /confirm permission synchronization/i }))
    await waitFor(() =>
      expect(requestPermissionSync).toHaveBeenNthCalledWith(2, 'revision-2', {
        target_scope: 'pool',
        target_id: 'pool-1',
        mode: 'apply',
        confirmed: true,
      }),
    )
  })

  it('creates an immutable revision and saves it as the pool default', async () => {
    vi.mocked(listPermissionTemplates).mockResolvedValue({ data: templates } as never)
    vi.mocked(createPermissionTemplateRevision).mockResolvedValue({
      data: {
        id: 'revision-3',
        revision: 3,
        privileges: ['SELECT', 'UPDATE'],
        grant_option: false,
        created_at: '2026-08-11T00:00:00Z',
      },
    } as never)
    vi.mocked(updateDedicatedPool).mockResolvedValue({ data: {} } as never)
    const changed = vi.fn()
    const user = userEvent.setup()

    render(
      <PermissionTemplateDrawer
        open
        poolId="pool-1"
        configRevision={2}
        selectedRevisionId="revision-2"
        onPoolUpdated={changed}
        onClose={vi.fn()}
      />,
    )

    await user.click(await screen.findByRole('button', { name: /create permission version/i }))
    await user.click(screen.getByRole('checkbox', { name: 'UPDATE' }))
    await user.click(screen.getByRole('button', { name: /create version/i }))
    await waitFor(() =>
      expect(createPermissionTemplateRevision).toHaveBeenCalledWith('template-1', {
        privileges: ['SELECT', 'INSERT', 'UPDATE'],
        grant_option: false,
      }),
    )

    await user.click(screen.getByRole('button', { name: /use for future agent accounts/i }))
    await waitFor(() =>
      expect(updateDedicatedPool).toHaveBeenCalledWith('pool-1', {
        expected_config_revision: 2,
        permission_template_revision_id: 'revision-3',
      }),
    )
    expect(changed).toHaveBeenCalled()
  })

  it('explains when no existing Agent accounts can be synchronized', async () => {
    vi.mocked(listPermissionTemplates).mockResolvedValue({ data: templates } as never)
    vi.mocked(requestPermissionSync).mockResolvedValue({
      data: {
        id: 'preview-empty',
        template_revision_id: 'revision-2',
        target_scope: 'pool',
        target_id: 'pool-1',
        mode: 'dry_run',
        status: 'succeeded',
        total_count: 0,
        completed_count: 0,
        failed_count: 0,
        failure_reason: null,
        targets: [],
      },
    } as never)
    const user = userEvent.setup()

    render(
      <PermissionTemplateDrawer
        open
        poolId="pool-1"
        configRevision={2}
        selectedRevisionId="revision-2"
        onClose={vi.fn()}
      />,
    )

    await user.click(
      await screen.findByRole('button', { name: /preview existing resources/i }),
    )
    expect(await screen.findByText(/no prepared Agent accounts exist/i))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /apply to existing accounts/i }))
      .not.toBeInTheDocument()
  })
})
