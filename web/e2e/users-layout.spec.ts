import { test, expect } from './fixtures'

for (const locale of ['en-US', 'zh-CN']) {
  test(`account table contains long identities and keeps actions reachable (${locale})`, async ({ adminPage: page, pas }) => {
    const name = 'Enterprise directory account with a long display name 用于布局回归的企业账号'
    await pas.seedDirectory()
    for (const width of [1440, 1024]) {
      await page.setViewportSize({ width, height: 900 })
      await page.goto(`${pas.url}/users`)
      if (locale === 'zh-CN') {
        await page.getByRole('button', { name: 'Switch language', exact: true }).click()
        await page.getByRole('menuitem', { name: '简体中文', exact: true }).click()
        await expect(page.getByRole('heading', { name: '用户', exact: true })).toBeVisible()
      }
      const row = page.getByRole('row').filter({ hasText: name })
      await expect(row).toBeVisible()
      const geometry = await row.evaluate(element => {
        const table = element.closest('.ant-table-container')!
        const nameCell = element.querySelector('td')!
        const text = nameCell.querySelector('strong')!
        return {
          pageWidth: document.documentElement.clientWidth,
          documentWidth: document.documentElement.scrollWidth,
          tableRight: table.getBoundingClientRect().right,
          nameWidth: nameCell.getBoundingClientRect().width,
          textHeight: text.getBoundingClientRect().height,
          roleHeight: element.querySelectorAll('td')[2].querySelector('.ant-tag')!.getBoundingClientRect().height,
        }
      })
      expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.pageWidth + 1)
      expect(geometry.tableRight).toBeLessThanOrEqual(geometry.pageWidth + 1)
      expect(geometry.nameWidth).toBeGreaterThanOrEqual(220)
      expect(geometry.textHeight).toBeLessThan(60)
      expect(geometry.roleHeight).toBeLessThan(30)
      // Playwright scrolls the table container to the operation before clicking.
      await row.getByRole('button', { name: 'Enterprise identity for ' + name, exact: true }).click()
      const identity = page.locator('section[aria-labelledby="user-enterprise-identity-heading"]')
      await expect(identity).toBeVisible()
      await identity.getByRole('button', { name: /Close Enterprise Identity|关闭企业身份/ }).click()
      await expect(identity).not.toBeVisible()
      await page.screenshot({ path: test.info().outputPath(`users-${width}.png`), fullPage: true })
    }
  })
}

test('DingTalk verification without a public URL explains configuration and remains retryable', async ({ adminPage: page, pas }) => {
  const created = await page.request.post(`${pas.url}/api/identity-sources`, {
    headers: { 'X-PAS-CSRF': '1' },
    data: { name: 'DingTalk callback regression', provider: 'dingtalk', tenant_id: 'synthetic-corp',
      client_id: 'synthetic-app', client_secret: 'synthetic-secret' },
  })
  expect(created.status()).toBe(201)
  await page.goto(`${pas.url}/users?tab=identity-sources`)
  const button = page.getByRole('button', { name: 'Verify DingTalk enterprise', exact: true })
  const response = page.waitForResponse(r => r.url().endsWith('/dingtalk-verification'))
  await button.click()
  expect((await response).status()).toBe(409)
  await expect(page.getByText('Configure runtime_policy.external_base_url as an absolute HTTP(S) URL before DingTalk authorization.', { exact: true })).toBeVisible()
  await expect(button).toBeEnabled()
  await expect(page).toHaveURL(`${pas.url}/users?tab=identity-sources`)
  expect((await page.context().cookies()).some(cookie => cookie.name === 'dingtalk_state')).toBe(false)
})
