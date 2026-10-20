import type { Locator, Page } from '@playwright/test'
import { test, expect, login, type PAS } from './fixtures'

const accessPath = '/settings/configuration?module=aliyun_access'
// Intentionally invalid, disposable values. Never use real cloud credentials here.
const accessKey = 'e2e-not-a-real-access-key'
const secret = 'e2e-not-a-real-access-key-secret'

async function selectRegion(page: Page, label: string, region: string) {
  const input = page.getByRole('combobox', { name: label })
  await input.fill(region)
  await page.locator('.ant-select-dropdown:visible')
    .getByText(new RegExp(' · ' + region + '$')).click()
}

async function expectRegion(input: Locator, region: string) {
  await expect(input.locator('xpath=ancestor::div[contains(@class, "ant-form-item-control")]').first())
    .toContainText(region)
}

async function chooseAccess(page: Page, pas: PAS, region: string, network: 'public' | 'vpc', fresh = false) {
  await page.goto(pas.url + accessPath)
  await page.getByRole('radio', { name: network === 'vpc' ? 'VPC (private)' : 'Public', exact: true }).check()
  await selectRegion(page, 'PAS access / default region', region)
  if (fresh) {
    await page.getByLabel('AccessKey ID', { exact: true }).fill(accessKey)
    await page.getByLabel('AccessKey secret', { exact: true }).fill(secret)
  }
}

async function preflight(page: Page) {
  const response = page.waitForResponse(response => response.url().endsWith('/api/config')
    && response.request().postDataJSON()?.action === 'plan')
  await page.getByRole('button', { name: 'Run dry-run', exact: true }).click()
  const result = await response
  expect(result.status()).toBe(200)
  const body = await result.json()
  expect(body.plan.error_code).toBe('OPENAPI_CONNECT_FAILURE')
  await expect(page.getByText('Dry-run checks failed', { exact: true })).toBeVisible()
  return { plan: body.plan, request: result.request().postDataJSON() }
}

async function activateDespiteBlockedNetwork(page: Page) {
  await page.getByRole('button', { name: 'Save and enable anyway', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('The connection check did not complete.')
  const activated = page.waitForResponse(response => response.url().endsWith('/api/config')
    && response.request().postDataJSON()?.action === 'activate')
  await dialog.getByRole('button', { name: 'Confirm activation', exact: true }).click()
  expect((await activated).status()).toBe(200)
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole('button', { name: 'Replace AccessKey', exact: true })).toBeVisible()
  // Activation persists first; the normal runtime poll publishes the snapshot.
  await expect.poll(async () => (await page.request.get(new URL('/readyz', page.url()).href)).status()).toBe(200)
}

async function catalog(page: Page, pas: PAS) {
  const response = await page.request.get(pas.url + '/api/config/aliyun-endpoints', { headers: { 'X-PAS-CSRF': '1' } })
  expect(response.status()).toBe(200)
  expect(response.headers()['cache-control']).toContain('no-store')
  return response.json()
}

test('OpenAPI requires explicit network and region, previews official endpoints and rejects catalog gaps', async ({ adminPage: page, pas, request }) => {
  await page.goto(pas.url + accessPath)
  const publicNetwork = page.getByRole('radio', { name: 'Public', exact: true })
  const vpcNetwork = page.getByRole('radio', { name: 'VPC (private)', exact: true })
  await expect(publicNetwork).not.toBeChecked()
  await expect(vpcNetwork).not.toBeChecked()
  await page.getByLabel('AccessKey ID', { exact: true }).fill(accessKey)
  await page.getByLabel('AccessKey secret', { exact: true }).fill(secret)
  await page.getByRole('button', { name: 'Run dry-run', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Select a supported region and network' })).toBeVisible()
  expect((await request.get(pas.url + '/api/config/aliyun-endpoints', { headers: { 'X-PAS-CSRF': '1' } })).status()).toBe(401)
  expect((await catalog(page, pas)).defaults).toBeNull()

  await publicNetwork.check()
  await selectRegion(page, 'PAS access / default region', 'cn-hangzhou')
  await expect(page.getByText('PolarDB · public: polardb.aliyuncs.com', { exact: true })).toBeVisible()
  await vpcNetwork.check()
  await expect(page.getByText('PolarDB · vpc: polardb-vpc.cn-hangzhou.aliyuncs.com', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /Official PolarDB endpoints/ }))
    .toHaveAttribute('href', 'https://api.aliyun.com/product/polardb?tab=endpoints')

  await publicNetwork.check()
  await selectRegion(page, 'PAS access / default region', 'cn-beijing-finance-1')
  await vpcNetwork.check()
  await expect(page.getByText('This region is unavailable for the selected service/network or PAS VPC access region.')).toBeVisible()
  await page.getByRole('button', { name: 'Run dry-run', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Select a supported region and network' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save and enable anyway', exact: true })).toHaveCount(0)
  const unsupported = await page.request.post(pas.url + '/api/config', {
    headers: { 'X-PAS-CSRF': '1' },
    data: { protocol_version: 1, action: 'plan', module: 'aliyun_access', config: {
      credential_mode: 'direct_ak', region_id: 'cn-beijing-finance-1', openapi_network: 'vpc',
      direct_ak: { access_key_id: accessKey, access_key_secret: secret },
    } },
  })
  expect((await unsupported.json()).plan).toMatchObject({
    valid: false, error_code: 'OPENAPI_ENDPOINT_UNSUPPORTED', confirmation_allowed: false,
  })

  await selectRegion(page, 'PAS access / default region', 'cn-hangzhou-finance')
  await page.getByRole('radio', { name: /^Assume RAM role/i }).check()
  await expect(page.getByText('This region is unavailable for the selected service/network or PAS VPC access region.')).toBeVisible()
  await selectRegion(page, 'PAS access / default region', 'cn-shenzhen')
  await expect(page.getByText('STS · vpc: sts-vpc.cn-shenzhen.aliyuncs.com', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Switch language', exact: true }).click()
  await page.getByRole('menuitem', { name: '简体中文', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'PAS 接入 / 默认地域' })).toBeVisible()
  await expect(page.getByText('华南1（深圳） · cn-shenzhen', { exact: true })).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: test.info().outputPath('endpoint-selection.png'), fullPage: true, animations: 'disabled' })
})

test('OpenAPI failed preflight keeps VPC diagnostics, invalidates edited plans and reuses stored credentials after restart', async ({ adminPage: page, pas }) => {
  await chooseAccess(page, pas, 'cn-shenzhen', 'vpc', true)
  const { plan } = await preflight(page)
  expect(plan.config).toMatchObject({ region_id: 'cn-shenzhen', openapi_network: 'vpc' })
  expect(plan.external_validation.checks).toEqual([expect.objectContaining({
    service: 'polardb', region_id: 'cn-shenzhen', network: 'vpc', stage: 'polardb',
    endpoint: 'polardb-vpc.cn-shenzhen.aliyuncs.com', status: 'FAILED',
    error_code: 'OPENAPI_CONNECT_FAILURE',
  })])
  expect(JSON.stringify(plan)).not.toContain(secret)
  await expect(page.getByText('Region: cn-shenzhen · Network: vpc · Stage: polardb', { exact: true })).toBeVisible()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: test.info().outputPath('vpc-preflight.png'), fullPage: true, animations: 'disabled' })
  await activateDespiteBlockedNetwork(page)
  expect((await catalog(page, pas)).defaults).toEqual({ region_id: 'cn-shenzhen', openapi_network: 'vpc' })
  await pas.restart()
  await login(page, pas)
  await page.goto(pas.url + accessPath)
  await expectRegion(page.getByRole('combobox', { name: 'PAS access / default region' }), 'cn-shenzhen')
  await expect(page.getByRole('radio', { name: 'VPC (private)', exact: true })).toBeChecked()
  await chooseAccess(page, pas, 'cn-hangzhou', 'public')
  await expect(page.getByLabel('AccessKey secret', { exact: true })).toHaveCount(0)
  const { request: reuse } = await preflight(page)
  expect(reuse.config).toMatchObject({
    credential_mode: 'direct_ak', region_id: 'cn-hangzhou', openapi_network: 'public',
    transition: { selected_mode_action: 'reuse_retained' },
  })
  expect(reuse.config.direct_ak).toBeUndefined()
  await page.getByRole('radio', { name: 'VPC (private)', exact: true }).check()
  await expect(page.getByRole('button', { name: 'Save and enable anyway', exact: true })).toHaveCount(0)
  await page.getByRole('radio', { name: 'Public', exact: true }).check()
  await selectRegion(page, 'PAS access / default region', 'cn-shanghai')
  await expect(page.getByText('Dry-run checks failed', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Save and enable anyway', exact: true })).toHaveCount(0)
  await preflight(page)
  await activateDespiteBlockedNetwork(page)
  await page.reload()
  await expectRegion(page.getByRole('combobox', { name: 'PAS access / default region' }), 'cn-shanghai')
  await expect(page.getByRole('radio', { name: 'Public', exact: true })).toBeChecked()
  expect((await catalog(page, pas)).defaults).toEqual({ region_id: 'cn-shanghai', openapi_network: 'public' })
})

async function poolNetworkStep(page: Page, pas: PAS, name: string) {
  await page.goto(pas.url + '/pool/dedicated/new')
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await page.getByLabel('Pool name', { exact: true }).fill(name)
  // This case never requests prewarming or purchases.
  await page.getByLabel('Prewarmed instance count', { exact: true }).fill('0')
  await page.getByRole('button', { name: 'Next', exact: true }).click()
}

test('resource region selectors share active defaults without rewriting existing pools or defaulting SQL-only instances', async ({ adminPage: page, pas }) => {
  await chooseAccess(page, pas, 'cn-shenzhen', 'public', true)
  await preflight(page)
  await activateDespiteBlockedNetwork(page)
  await poolNetworkStep(page, pas, 'E2E region pool')
  await expectRegion(page.getByRole('combobox', { name: 'Region ID' }), 'cn-shenzhen')
  await page.getByLabel('Zone ID', { exact: true }).fill('cn-shenzhen-e2e')
  await page.getByLabel('VPC ID', { exact: true }).fill('vpc-e2e')
  await page.getByLabel('vSwitch ID', { exact: true }).fill('vsw-e2e')
  await selectRegion(page, 'Region ID', 'cn-hangzhou')
  for (const label of ['Zone ID', 'VPC ID', 'vSwitch ID']) {
    await expect(page.getByLabel(label, { exact: true })).toHaveValue('')
  }
  await page.getByLabel('Zone ID', { exact: true }).fill('cn-hangzhou-e2e')
  await page.getByLabel('VPC ID', { exact: true }).fill('vpc-e2e-hangzhou')
  await page.getByLabel('vSwitch ID', { exact: true }).fill('vsw-e2e-hangzhou')
  const createRequests: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/api/dedicated-pools') && request.method() === 'POST') {
      createRequests.push(request.url())
    }
  })
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Save as not started', exact: true })).toBeVisible()
  expect(createRequests).toEqual([])
  const created = page.waitForResponse(response => response.url().endsWith('/api/dedicated-pools')
    && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Save as not started', exact: true }).click()
  const createdResponse = await created
  expect(createdResponse.ok()).toBe(true)
  expect(createRequests).toHaveLength(1)
  const pool = await createdResponse.json()
  expect(pool).toMatchObject({ region_id: 'cn-hangzhou', target_size: 0 })
  await expect(page).toHaveURL(pas.url + '/pool/dedicated/' + pool.id)

  await chooseAccess(page, pas, 'cn-shenzhen', 'vpc')
  await preflight(page)
  await activateDespiteBlockedNetwork(page)
  const saved = await page.request.get(pas.url + '/api/dedicated-pools/' + pool.id)
  expect(await saved.json()).toMatchObject({ region_id: 'cn-hangzhou', vpc_id: 'vpc-e2e-hangzhou', members: [] })
  await page.goto(pas.url + '/pool/dedicated/' + pool.id)
  await page.getByRole('button', { name: /Edit pool configuration$/ }).click()
  await page.getByRole('button', { name: /Advanced network configuration$/ }).click()
  await expectRegion(page.getByRole('combobox', { name: 'Region ID' }), 'cn-hangzhou')
  await expect(page.getByText('This region is unavailable for the selected service/network or PAS VPC access region.')).toBeVisible()
  await poolNetworkStep(page, pas, 'E2E VPC region pool')
  await expectRegion(page.getByRole('combobox', { name: 'Region ID' }), 'cn-shenzhen')
  await expect(page.getByText('VPC OpenAPI: resource region must be cn-shenzhen.', { exact: true })).toBeVisible()
  await page.getByRole('combobox', { name: 'Region ID' }).fill('cn-hangzhou')
  await expect(page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: / · cn-hangzhou$/ }))
    .toHaveClass(/ant-select-item-option-disabled/)
  await page.keyboard.press('Escape')
  await page.goto(pas.url + '/instances')
  // The loaded empty state also has a registration action; select the header action.
  await page.locator('.page-container-header').getByRole('button', { name: /Register Instance$/ }).click()
  const regionInput = page.getByRole('combobox', { name: 'Region' })
  await expect(regionInput).toHaveValue('')
  await expect(page.getByRole('dialog').getByText('Select a region', { exact: true })).toBeVisible()
  await expect(page.getByText("Optional for SQL connections. Required for cloud OpenAPI operations; select the instance's actual region.")).toBeVisible()
  // A SQL-only registration can describe a different region; OpenAPI use is separately guarded.
  await selectRegion(page, 'Region', 'cn-hangzhou')
  await expectRegion(regionInput, 'cn-hangzhou')
  await page.screenshot({ path: test.info().outputPath('instance-region.png'), fullPage: true, animations: 'disabled' })
})
