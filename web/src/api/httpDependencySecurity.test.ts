/// <reference types="node" />
// @vitest-environment node
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { describe, it } from 'vitest'

const fixture = fileURLToPath(new URL('../test/http-dependency-security.mjs', import.meta.url))
const cases = [
  ['GHSA-542g-h47m-68v8', 'http2-session-error'],
  ['GHSA-3pq3-5fj3-cg6v lookup', 'http2-lookup-policy'],
  ['GHSA-3pq3-5fj3-cg6v proxy', 'http2-proxy-policy'],
  ['GHSA-x97p-jq2g-jp4f', 'form-serializer-options'],
  ['GHSA-mghh-pgcx-3jjj', 'proxy-hostname-complexity'],
  ['GHSA-c29m-xwm3-cm6r', 'data-uri-complexity'],
  ['GHSA-9fr6-4gfg-395g', 'default-request-method'],
  ['GHSA-vh66-26gq-q6x8', 'fetch-options-headers'],
  ['GHSA-r4gj-5m52-g5wh', 'fetch-redirect-limit'],
  ['GHSA-44g4-m2mj-wpvx', 'cidr-proxy-exclusion'],
  ['GHSA-m8m8-qj5v-23w3', 'socket-creation-option'],
  ['GHSA-4hqw-qxg8-jxx2', 'fetch-form-data-headers'],
  ['GHSA-j8rh-479h-cp32', 'interceptor-replacement-headers'],
  ['GHSA-8r9p-f939-6h3c', 'redirect-header-metacharacters'],
  ['GHSA-q3jh-wrc6-jgq8', 'redirect-array-headers'],
]

describe('HTTP dependency security boundaries', () => {
  it.each(cases)('%s: %s', (_advisory, name) => {
    const env = { ...process.env }
    for (const key of Object.keys(env)) {
      if (/^(https?|all|no)_proxy$/i.test(key)) delete env[key]
    }
    // A child process isolates prototype mutations and bounds malformed-input
    // CPU regressions even when the dependency blocks its event loop.
    try {
      execFileSync(process.execPath, [fixture, name], {
        env, timeout: 4000, encoding: 'utf8', stdio: 'pipe',
      })
    } catch (error) {
      const failure = error as { code?: string; status?: number; stderr?: string }
      throw new Error(`${name} failed (${failure.code ?? failure.status}): ${String(failure.stderr ?? '').slice(0, 2000)}`)
    }
  })
})
