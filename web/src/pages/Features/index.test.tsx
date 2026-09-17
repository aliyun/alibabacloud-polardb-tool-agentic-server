import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import Features from './index'
import userEvent from '@testing-library/user-event'
import { executeConfig } from '../../api/configuration'
import api from '../../api/client'
import LocaleProvider from '../../i18n/LocaleProvider'
import { createTestI18n } from '../../i18n/i18n'

vi.mock('../../api/client', () => ({ default: { get: vi.fn(), post: vi.fn() }, getAPIErrorMessage: () => 'Request failed' }))
vi.mock('../../api/configuration', () => ({ executeConfig: vi.fn() }))
const base = { state: 'DISABLED', managed: false, available: false, desired_enabled: false, desired_revision: 1,
  loaded_revision: 1, error_code: null, replicas: [], blockers: { in_flight: 0, pending_cleanup: 0, pending_uploads: 0, catalog_syncs: 0 } }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(executeConfig).mockReset() })
function show(status = base) {
  vi.mocked(api.get).mockImplementation(async url => ({ data: String(url).endsWith('/options') ? { connections: [], resources: [], users: [] } : status }))
  return render(<LocaleProvider i18nInstance={createTestI18n('en-US')}><MemoryRouter><Features /></MemoryRouter></LocaleProvider>)
}
it('offers onboarding for a fresh installation without announcing enabled tools', async () => {
  show()
  expect(await screen.findByRole('button', { name: 'Enable knowledge' })).toBeEnabled()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})
it('opens managed onboarding and explains the control-plane restart', async () => {
  show({ ...base, managed: true })
  await userEvent.click(await screen.findByRole('button', { name: 'Enable knowledge' }))
  expect(await screen.findByText('Set up knowledge')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled()
  expect(screen.getByText(/Restart instance/)).toBeInTheDocument()
})
it('reserves managed fleet confirmation for the controller', async () => {
  show({ ...base, managed: true, state: 'ACTIVATING', desired_enabled: true })
  expect(await screen.findByText(/control plane.*verifies/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm all replicas are ready' })).not.toBeInTheDocument()
})
it('retains self-hosted fleet confirmation', async () => {
  show({ ...base, state: 'ACTIVATING', desired_enabled: true })
  expect(await screen.findByRole('button', { name: 'Confirm all replicas are ready' })).toBeEnabled()
})
it('saves managed configuration over HTTP without randomUUID', async () => {
  vi.stubGlobal('crypto', { getRandomValues: (values: Uint8Array) => values.fill(7) })
  try {
    vi.mocked(executeConfig).mockResolvedValue({ module: { revision: 2, effective: { config: { enabled: true } } },
      validation: { validation_id: 'proof' } } as never)
    show({ ...base, managed: true, state: 'FAILED', desired_enabled: true })
    await userEvent.click(await screen.findByRole('button', { name: 'Save disabled state' }))
    expect(await screen.findByText(/Configuration saved.*Restart instance/)).toBeInTheDocument()
    expect(executeConfig).toHaveBeenCalledWith(expect.objectContaining({ action: 'activate', idempotency_key: expect.any(String) }))
    expect(api.post).not.toHaveBeenCalled()
  } finally { vi.unstubAllGlobals() }
})
it('prevents disable while an external operation remains unresolved', async () => {
  show({ ...base, state: 'DRAINING', desired_enabled: true, blockers: { ...base.blockers, pending_cleanup: 1 } })
  expect(await screen.findByRole('button', { name: /Save disabled/i })).toBeDisabled()
})
it('can return to disabled configuration after startup verification fails', async () => {
  show({ ...base, state: 'FAILED', desired_enabled: true })
  expect(await screen.findByRole('button', { name: /Save disabled/i })).toBeEnabled()
})
