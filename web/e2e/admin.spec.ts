import { test, expect, login } from './fixtures'

test('fresh install, login, reload and service restart preserve administrator access', async ({ adminPage: page, pas }) => {
  const ready = await page.request.get(`${pas.url}/readyz`)
  expect((await ready.json()).mode).toBe('READY')
  await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible()
  await expect(page.getByText('Total Users', { exact: true })).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('dashboard.png'), fullPage: true, animations: 'disabled' })
  await pas.restart()
  await login(page, pas)
  expect((await (await page.request.get(`${pas.url}/auth/me`)).json()).role).toBe('admin')
})

test('administrator creates, edits and deletes a department through UI', async ({ adminPage: page, pas }) => {
  await page.goto(`${pas.url}/departments`)
  await page.locator('.page-container-header').getByRole('button', { name: /New Department$/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Name', { exact: true }).fill('E2E Department')
  await dialog.getByLabel('Description', { exact: true }).fill('Created in browser')
  await dialog.getByRole('button', { name: 'OK', exact: true }).click()
  let row = page.getByRole('row').filter({ hasText: 'E2E Department' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: /Edit$/ }).click()
  await dialog.getByLabel('Name', { exact: true }).fill('E2E Renamed')
  await dialog.getByRole('button', { name: 'OK', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: 'E2E Renamed' })).toBeVisible()
  await page.reload()
  row = page.getByRole('row').filter({ hasText: 'E2E Renamed' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: /Delete$/ }).click()
  await page.getByRole('button', { name: 'OK', exact: true }).click()
  await expect(row).toHaveCount(0)
})

test('administrator creates a member, who can sign in but cannot administer', async ({ adminPage: page, pas, browser }) => {
  await page.goto(`${pas.url}/users`)
  await page.locator('.page-container-header').getByRole('button', { name: /Create User$/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Username', { exact: true }).fill('e2e-member')
  await dialog.getByLabel('Display Name', { exact: true }).fill('E2E Member')
  await dialog.getByLabel('Initial Password', { exact: true }).fill(pas.password)
  await dialog.getByLabel('Confirm Password', { exact: true }).fill(pas.password)
  await dialog.getByRole('button', { name: 'Create', exact: true }).click()
  await expect(page.getByRole('row').filter({ hasText: 'e2e-member' })).toBeVisible()
  const context = await browser.newContext({ locale: 'en-US' })
  try {
    const member = await context.newPage()
    await login(member, pas, 'e2e-member')
    expect((await context.request.get(`${pas.url}/api/users`)).status()).toBe(403)
    await member.goto(`${pas.url}/settings/features`)
    await expect(member).not.toHaveURL(/\/settings\/features/)
  } finally { await context.close() }
})

test('administrator creates and disables an Agent through UI', async ({ adminPage: page, pas }) => {
  await page.goto(`${pas.url}/agents`)
  await page.locator('.page-container-header').getByRole('button', { name: /Create Agent$/ }).click()
  await page.getByLabel('Name', { exact: true }).fill('E2E Agent')
  await page.getByLabel('Description', { exact: true }).fill('Browser lifecycle coverage')
  await page.getByRole('button', { name: 'Save Agent', exact: true }).click()
  await page.getByRole('button', { name: 'Disable E2E Agent', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm disable', exact: true }).click()
  await expect(page.getByRole('row').filter({ hasText: 'E2E Agent' })).toContainText('Disabled')
  await page.reload()
  await expect(page.getByRole('row').filter({ hasText: 'E2E Agent' })).toContainText('Disabled')
})

test('knowledge feature saves a connection draft but gates activation on verified access', async ({ adminPage: page, pas }) => {
  await page.goto(`${pas.url}/settings/features`)
  await expect(page.getByRole('heading', { name: 'Features', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Enable knowledge', exact: true }).click()
  await expect(page.getByRole('button', { name: /Save and enable$/ })).toBeDisabled()
  await page.getByRole('button', { name: 'Add connection', exact: true }).click()
  await page.getByLabel('Connection name', { exact: true }).fill('E2E Draft')
  await page.getByLabel('Host', { exact: true }).fill('127.0.0.1')
  await page.getByLabel('Username', { exact: true }).fill('e2e')
  await page.getByLabel('Password', { exact: true }).fill(pas.password)
  await page.getByRole('button', { name: 'Save connection draft', exact: true }).click()
  await expect(page.getByText('Connection draft saved. Check it when the upstream service is ready.')).toBeVisible()
  // The loading icon contributes "loading" to the accessible name.
  await expect(page.getByRole('button', { name: /Save and enable$/ })).toBeDisabled()
  await page.reload()
  await page.getByRole('button', { name: 'Enable knowledge', exact: true }).click()
  await page.getByRole('combobox', { name: 'Upstream connection' }).click()
  await expect(page.getByText('E2E Draft', { exact: true })).toBeVisible()
  const status = await (await page.request.get(`${pas.url}/api/features/knowledge`)).json()
  expect(status.available).toBe(false)
})

test('real HTTP listener rejects anonymous business and discovery requests', async ({ adminPage: page, pas, request }) => {
  for (const path of ['/api/users', '/api/agents', '/mcp/rest/tools', '/help/enterprise-identity-sources']) {
    expect((await request.get(pas.url + path)).status(), path).toBe(401)
  }
  for (const path of ['/docs', '/redoc', '/openapi.json', '/api/openapi.json', '/mcp/rest/docs', '/mcp/rest/openapi.json']) {
    expect((await request.get(pas.url + path, { headers: { Accept: 'text/html' } })).status(), path).toBe(404)
  }
  expect((await page.request.get(`${pas.url}/mcp/rest/tools`)).status()).toBe(200)
})
