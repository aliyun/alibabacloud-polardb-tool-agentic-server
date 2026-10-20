// Smoke the real source/target UI while the local maintenance runner owns it.
import process from 'node:process'
import { chromium, expect } from '@playwright/test'

const url = new URL(process.env.PAS_MAINT_URL)
if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') throw new Error('Owned loopback URL required')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
try {
  const context = await browser.newContext({ locale: 'en-US', viewport: { width: 1440, height: 1000 } })
  await context.route('**/*', route => {
    const target = new URL(route.request().url())
    return ['127.0.0.1', 'localhost'].includes(target.hostname) || ['data:', 'blob:'].includes(target.protocol)
      ? route.continue() : route.abort('blockedbyclient')
  })
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => localStorage.setItem('pas.ui.locale', 'en-US'))
  await page.goto(new URL('/login', url).href)
  await page.getByPlaceholder('Username', { exact: true }).fill('maintenance-admin')
  await page.getByPlaceholder('Password', { exact: true }).fill(process.env.PAS_MAINT_PASSWORD)
  await page.getByRole('button', { name: 'Sign In', exact: true }).click()
  await expect(page).not.toHaveURL(/\/login/)
  await page.goto(new URL('/agents', url).href)
  await expect(page.getByRole('row').filter({ hasText: 'Maintenance source retained Agent' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('row').filter({ hasText: 'Maintenance source retained Agent' })).toBeVisible()
  const response = await context.request.get(new URL('/auth/me', url).href)
  expect(response.status()).toBe(200)
  expect((await response.json()).role).toBe('admin')
  await page.screenshot({ path: process.env.PAS_MAINT_SCREENSHOT, fullPage: true, animations: 'disabled' })
  expect(errors).toEqual([])
  console.log(JSON.stringify({ browser: browser.version(), retainedAgentVisible: true, reloadAuthenticated: true }))
} finally {
  await browser.close()
}
