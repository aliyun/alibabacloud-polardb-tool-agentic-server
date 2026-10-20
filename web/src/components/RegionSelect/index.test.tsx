import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { getEndpointCatalog } from '../../api/aliyunEndpoints'
import { endpointCatalog } from '../../test/endpointCatalog'
import { i18n } from '../../i18n/i18n'
import RegionSelect from './index'
import { regionAvailable } from './useEndpointCatalog'

vi.mock('../../api/aliyunEndpoints', () => ({ getEndpointCatalog: vi.fn() }))

it('uses the same localized reviewed region labels', async () => {
  await i18n.changeLanguage('zh-CN')
  render(<RegionSelect catalog={endpointCatalog} value="cn-shenzhen" network="vpc" />)
  expect(screen.getByText('华南1（深圳） · cn-shenzhen')).toBeInTheDocument()
  expect(screen.getByText(/polardb-vpc\.cn-shenzhen\.aliyuncs\.com/)).toBeInTheDocument()
})

it('enforces VPC placement and the independent STS availability', () => {
  expect(regionAvailable(endpointCatalog, 'cn-beijing', 'vpc', false, 'cn-hangzhou')).toBe(false)
  expect(regionAvailable(endpointCatalog, 'cn-hangzhou', 'vpc', false, 'cn-hangzhou')).toBe(true)
  expect(regionAvailable(endpointCatalog, 'cn-hangzhou-finance', 'vpc')).toBe(true)
  expect(regionAvailable(endpointCatalog, 'cn-hangzhou-finance', 'vpc', true)).toBe(false)
  expect(regionAvailable(endpointCatalog, 'cn-unknown', 'public')).toBe(false)
})

it('shows a retryable catalog failure without enabling a public fallback', async () => {
  vi.mocked(getEndpointCatalog).mockRejectedValueOnce(new Error('unavailable'))
  vi.mocked(getEndpointCatalog).mockResolvedValueOnce(endpointCatalog)
  const user = userEvent.setup()
  render(<RegionSelect aria-label="Region" />)
  expect(await screen.findByText(/catalog could not be loaded/i)).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Region' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Region' })).not.toBeDisabled())
})
