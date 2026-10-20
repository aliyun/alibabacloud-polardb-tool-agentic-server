import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../../api/client'
import EtlPermissionsPanel from './EtlPermissionsPanel'

vi.mock('../../api/client', () => ({ default: { get: vi.fn(), post: vi.fn() }, getAPIErrorMessage: (_error: unknown, fallback: string) => fallback }))
beforeEach(() => vi.clearAllMocks())

it('verifies and fetches permissions through separate operations and displays import counts', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { state: 'SUCCEEDED', connection: { state: 'SUCCEEDED' }, sync: { synced_at: '2026-10-12T10:00:00Z', snapshots_processed: 4, snapshots_skipped: 1 } } })
  vi.mocked(api.post).mockResolvedValue({ status: 202 })
  render(<EtlPermissionsPanel sourceId="source-1" />)
  const verify = await screen.findByRole('button', { name: 'Verify connection' })
  await waitFor(() => expect(verify).toBeEnabled())
  fireEvent.click(verify)
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/identity-sources/source-1/etl-permissions/verify'))
  const sync = screen.getByRole('button', { name: 'Sync permissions now' })
  await waitFor(() => expect(sync).toBeEnabled())
  fireEvent.click(sync)
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/identity-sources/source-1/etl-permissions/sync'))
  expect(await screen.findByText('4 / 1')).toBeInTheDocument()
})

it('prevents duplicate triggers while the ETL job is running', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { state: 'RUNNING', connection: { state: 'UNVERIFIED' } } })
  render(<EtlPermissionsPanel sourceId="source-1" />)
  expect(await screen.findByText('Running')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Verify connection' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Sync permissions now' })).toBeDisabled()
  expect(api.post).not.toHaveBeenCalled()
})
