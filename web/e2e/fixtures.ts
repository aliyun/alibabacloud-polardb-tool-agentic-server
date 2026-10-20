import { test as base, expect, type Page } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const python = process.env.E2E_PYTHON || (existsSync(path.join(root, '.venv/bin/python'))
  ? path.join(root, '.venv/bin/python') : 'python3')

export interface PAS {
  url: string
  bootstrapToken: string
  password: string
  restart: () => Promise<void>
  seedDirectory: () => Promise<void>
}

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('No loopback port allocated')
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  return address.port
}

async function stop(child: ChildProcess | undefined) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return
  const exited = new Promise<void>(resolve => child.once('exit', () => resolve()))
  child.kill('SIGTERM')
  const timer = setTimeout(() => child.kill('SIGKILL'), 10_000)
  try { await exited } finally { clearTimeout(timer) }
}

export async function login(page: Page, pas: PAS, username = 'admin', password = pas.password) {
  await page.context().clearCookies()
  await page.goto(`${pas.url}/login`)
  await page.getByPlaceholder('Username', { exact: true }).fill(username)
  await page.getByPlaceholder('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign In', exact: true }).click()
  await expect(page).not.toHaveURL(/\/login/)
}

export async function initialize(page: Page, pas: PAS) {
  await page.goto(pas.url)
  await expect(page.getByRole('heading', { name: 'Claim this installation' })).toBeVisible()
  await page.getByLabel('Bootstrap token', { exact: true }).fill(pas.bootstrapToken)
  await page.getByRole('button', { name: 'Verify and continue' }).click()
  await page.locator('#username').fill('admin')
  await page.locator('#password').fill(pas.password)
  await page.getByRole('button', { name: 'Run dry-run', exact: true }).click()
  await page.getByRole('button', { name: 'Activate module', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Enter administration console' })).toBeEnabled()
  await expect.poll(async () => (await page.request.get(`${pas.url}/readyz`)).status()).toBe(200)
  await page.getByRole('button', { name: 'Enter administration console' }).click()
  await expect(page).toHaveURL(`${pas.url}/dashboard`)
}

export const test = base.extend<{ pas: PAS; adminPage: Page }>({
  // Playwright requires a destructured fixture argument even when no fixtures are used.
  // eslint-disable-next-line no-empty-pattern
  pas: [async ({}, runCase, testInfo) => {
    // Never accept an existing URL or metadata DB: each case owns its process and DB.
    const directory = await mkdtemp(path.join(tmpdir(), 'pas-browser-e2e-'))
    const port = await freePort()
    const url = `http://127.0.0.1:${port}`
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
      !key.startsWith('PAS_') && !key.startsWith('ALIBABA_CLOUD_')
      && !['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'pythonpath'].includes(key.toLowerCase())))
    Object.assign(env, {
      PAS_DATABASE_URL: `sqlite+aiosqlite:///${directory}/metadata.db`,
      PAS_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
      PAS_SERVER_PORT: String(port),
      PAS_STATIC_DIR: path.join(root, 'web/dist'),
      PAS_E2E_OWNED_DIRECTORY: directory,
      PYTHONUNBUFFERED: '1',
      NO_PROXY: '127.0.0.1,localhost',
    })
    let logs = ''
    let child: ChildProcess | undefined
    let processError: Error | undefined
    const launch = (args: string[]) => {
      const process = spawn(python, [path.join(root, 'web/e2e/pas_runner.py'), ...args], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] })
      process.stdout!.on('data', data => { logs += data.toString() })
      process.stderr!.on('data', data => { logs += data.toString() })
      process.on('error', error => { processError = error })
      return process
    }
    const start = async () => {
      child = launch(['serve', '--local-sso-dev'])
      await expect.poll(async () => {
        if (processError) throw processError
        if (child!.exitCode !== null) throw new Error('PAS exited before readiness; see server.log')
        try { return (await fetch(`${url}/readyz`)).status } catch { return 0 }
      }, { timeout: 30_000 }).toBe(200)
    }
    try {
      if (!existsSync(path.join(root, 'web/dist/index.html'))) throw new Error('Build web first: npm run build')
      child = launch(['database', 'migrate'])
      const migration = child
      await expect.poll(() => {
        if (processError) throw processError
        return migration.exitCode
      }, { timeout: 30_000 }).not.toBeNull()
      expect(migration.exitCode, 'Database migration must succeed').toBe(0)
      await start()
      const token = logs.match(/Bootstrap token: (\S+)/)?.[1]
      if (!token) throw new Error('Fresh PAS did not issue a bootstrap token; see server.log')
      await runCase({ url, bootstrapToken: token, password: `E2e!${randomBytes(16).toString('hex')}`,
        restart: async () => { await stop(child); await start() },
        seedDirectory: async () => {
          const seed = launch(['seed-layout'])
          try {
            await expect.poll(() => {
              if (processError) throw processError
              return seed.exitCode
            }, { timeout: 15_000 }).not.toBeNull()
            expect(seed.exitCode, 'Synthetic directory must be persisted in the private database').toBe(0)
          } finally { await stop(seed) }
        },
      })
    } finally {
      await stop(child)
      const logPath = testInfo.outputPath('server.log')
      await writeFile(logPath, logs.replace(/Bootstrap token: \S+/g, 'Bootstrap token: [REDACTED]'), { mode: 0o600 })
      await testInfo.attach('PAS service log', { path: logPath, contentType: 'text/plain' })
      // Only this mkdtemp-created directory is removed; no external DB or path is accepted.
      await rm(directory, { recursive: true, force: true })
    }
  }, { timeout: 90_000 }],
  adminPage: async ({ page, pas }, runCase) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => localStorage.setItem('pas.ui.locale', 'en-US'))
    await initialize(page, pas)
    await runCase(page)
    expect(errors, 'Browser must not have unhandled JavaScript errors').toEqual([])
  },
})
export { expect }
