// Each case runs in a separate, time-bounded Node process. All network peers
// are local fixtures; no production service or external DNS target is used.
import assert from 'node:assert/strict'
import { once } from 'node:events'
import http from 'node:http'
import http2 from 'node:http2'
import net from 'node:net'
import { createRequire } from 'node:module'
import axios from 'axios'

const requireAxios = createRequire(import.meta.resolve('axios'))
const followRedirects = requireAxios('follow-redirects')
const servers = []
const sessions = new Set()

async function listen(server, host = '127.0.0.1') {
  servers.push(server)
  server.listen(0, host)
  await once(server, 'listening')
  return `http://${host}:${server.address().port}`
}

async function echo() {
  return listen(http.createServer((req, res) => {
    req.resume()
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ headers: req.headers, method: req.method }))
  }))
}

async function polluted(entries, action) {
  const originals = entries.map(([key]) => Object.getOwnPropertyDescriptor(Object.prototype, key))
  try {
    for (const [key, value] of entries) {
      Object.defineProperty(Object.prototype, key, { value, configurable: true, writable: true })
    }
    return await action()
  } finally {
    entries.forEach(([key], index) => {
      if (originals[index]) Object.defineProperty(Object.prototype, key, originals[index])
      else delete Object.prototype[key]
    })
  }
}

async function h2Origin() {
  let hits = 0
  const server = http2.createServer()
  server.on('session', (session) => {
    sessions.add(session)
    session.on('close', () => sessions.delete(session))
  })
  server.on('stream', (stream) => {
    hits++
    stream.respond({ ':status': 200 })
    stream.end('origin')
  })
  const url = await listen(server, 'localhost')
  return { url, hits: () => hits }
}

async function followedHeaders(headers, sensitiveHeaders) {
  const target = await echo()
  const redirect = await listen(http.createServer((req, res) => {
    req.resume()
    res.writeHead(302, { Location: target })
    res.end()
  }))
  const host = new URL(redirect).host
  if (Array.isArray(headers)) headers.unshift('Host', host)
  else headers = { ...headers, Host: host }
  return new Promise((resolve, reject) => {
    const request = followRedirects.http.get(redirect, { headers, sensitiveHeaders }, (res) => {
      let body = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { body += chunk })
      res.on('end', () => resolve(JSON.parse(body).headers))
      res.on('error', reject)
    })
    request.on('error', reject)
    request.setTimeout(1000, () => request.destroy(new Error('fixture request timed out')))
  })
}

const cases = {
  async 'http2-session-error'() {
    const server = net.createServer()
    const url = await listen(server)
    await new Promise((resolve) => server.close(resolve))
    await assert.rejects(axios.get(url, { httpVersion: 2, proxy: false, timeout: 500 }))
    // A rejected request must not leave an unhandled session error behind.
    await new Promise((resolve) => setTimeout(resolve, 50))
  },

  async 'http2-lookup-policy'() {
    const origin = await h2Origin()
    let lookups = 0
    await assert.rejects(axios.get(origin.url, {
      httpVersion: 2, proxy: false, timeout: 500,
      lookup(_host, _options, callback) {
        lookups++
        callback(Object.assign(new Error('blocked by test policy'), { code: 'EPOLICY' }))
      },
    }))
    assert.ok(lookups > 0)
    assert.equal(origin.hits(), 0)
    const control = await axios.get(origin.url, { httpVersion: 2, proxy: false, timeout: 500 })
    assert.equal(control.data, 'origin')
    assert.equal(origin.hits(), 1)
  },

  async 'http2-proxy-policy'() {
    const origin = await h2Origin()
    let proxyHits = 0
    const proxy = await listen(http.createServer((req, res) => {
      proxyHits++
      req.resume()
      res.writeHead(502)
      res.end()
    }))
    const proxyUrl = new URL(proxy)
    await assert.rejects(axios.get(origin.url, {
      httpVersion: 2, timeout: 500,
      proxy: { host: proxyUrl.hostname, port: Number(proxyUrl.port), protocol: 'http' },
    }), { code: 'ERR_NOT_SUPPORT' })
    assert.equal(origin.hits(), 0, 'HTTP/2 must not bypass a mandatory proxy')
    assert.equal(proxyHits, 0, 'an unsupported HTTP/2 proxy must fail before dispatch')
  },

  async 'form-serializer-options'() {
    const fields = await polluted([['maxDepth', 1]], () =>
      Array.from(axios.toFormData({ a: { b: { c: 'value' } } }, new FormData()).entries()))
    assert.deepEqual(fields, [['a[b][c]', 'value']])
  },

  async 'proxy-hostname-complexity'() {
    const proxy = await listen(http.createServer({ maxHeaderSize: 1_000_000 }, (req, res) => {
      req.resume()
      res.end('proxy')
    }))
    process.env.HTTP_PROXY = proxy
    process.env.NO_PROXY = 'example.invalid'
    const response = await axios.get(`http://${'.'.repeat(220_000)}a/`, { timeout: 1000 })
    assert.equal(response.data, 'proxy')
  },

  async 'data-uri-complexity'() {
    await assert.rejects(axios.get(`data:${'/'.repeat(220_000)}`, { proxy: false, timeout: 1000 }))
    const valid = await axios.get('data:text/plain;base64,b2s=', { responseType: 'text' })
    assert.equal(valid.data, 'ok')
  },

  async 'default-request-method'() {
    const url = await echo()
    const response = await polluted([['method', 'DELETE']], () =>
      axios.request({ url, proxy: false, timeout: 1000 }))
    assert.equal(response.data.method, 'GET')
  },

  async 'fetch-options-headers'() {
    const url = await echo()
    const response = await polluted([['headers', { Authorization: 'test-polluted' }]], () =>
      axios.get(url, {
        adapter: 'fetch', timeout: 1000,
        headers: { 'X-Good': 'yes' }, fetchOptions: {},
      }))
    assert.equal(response.data.headers['x-good'], 'yes')
    assert.equal(response.data.headers.authorization, undefined)
  },

  async 'fetch-redirect-limit'() {
    let targetHits = 0
    const target = await listen(http.createServer((req, res) => {
      targetHits++
      req.resume()
      res.end('target')
    }))
    const redirect = await listen(http.createServer((req, res) => {
      req.resume()
      res.writeHead(302, { Location: target })
      res.end()
    }))
    const response = await axios.get(redirect, {
      adapter: 'fetch', maxRedirects: 0, timeout: 1000, validateStatus: () => true,
    })
    assert.equal(targetHits, 0)
    assert.equal(response.status, 302)
    const control = await axios.get(redirect, { adapter: 'fetch', timeout: 1000 })
    assert.equal(control.data, 'target')
    assert.equal(targetHits, 1)
  },

  async 'cidr-proxy-exclusion'() {
    let proxyHits = 0
    const origin = await listen(http.createServer((req, res) => {
      req.resume()
      res.end('origin')
    }))
    process.env.HTTP_PROXY = await listen(http.createServer((req, res) => {
      proxyHits++
      req.resume()
      res.end('proxy')
    }))
    process.env.NO_PROXY = '127.0.0.0/8'
    assert.equal((await axios.get(origin, { timeout: 1000 })).data, 'origin')
    assert.equal(proxyHits, 0)
    process.env.NO_PROXY = '192.0.2.0/24'
    assert.equal((await axios.get(origin, { timeout: 1000 })).data, 'proxy')
    assert.equal(proxyHits, 1)
  },

  async 'socket-creation-option'() {
    let attackerHits = 0
    const attacker = new URL(await listen(http.createServer((req, res) => {
      attackerHits++
      req.resume()
      res.end('attacker')
    })))
    const origin = await echo()
    const response = await polluted([['createConnection', (_options, callback) => {
      const socket = net.connect({ host: '127.0.0.1', port: Number(attacker.port) }, () => {
        if (typeof callback === 'function') callback(null, socket)
      })
      return socket
    }]], () => axios.get(origin, {
      proxy: false, timeout: 1000, headers: { Authorization: 'test-credential' },
    }))
    assert.equal(attackerHits, 0)
    assert.equal(response.data.headers.authorization, 'test-credential')
  },

  async 'fetch-form-data-headers'() {
    const url = await echo()
    const response = await polluted([
      [Symbol.toStringTag, 'FormData'],
      ['append', () => {}],
      ['getHeaders', () => ({ 'X-Poisoned': 'yes' })],
    ], () => axios.post(url, ['a', 'b'], { adapter: 'fetch', timeout: 1000 }))
    assert.equal(response.data.headers['x-poisoned'], undefined)
    assert.equal(response.data.method, 'POST')
  },

  async 'interceptor-replacement-headers'() {
    const url = await echo()
    const client = axios.create()
    client.interceptors.request.use((config) => ({
      url: config.url, method: config.method, timeout: 1000, proxy: false,
    }))
    const response = await polluted([['headers', { 'X-Poisoned': 'yes' }]], () => client.get(url))
    // The minimal interceptor also drops transformResponse; decode explicitly
    // so this case measures header safety rather than JSON configuration.
    const data = typeof response.data === 'string' ? JSON.parse(response.data) : response.data
    assert.equal(data.headers['x-poisoned'], undefined)
    assert.equal(data.method, 'GET')
  },

  async 'redirect-header-metacharacters'() {
    const headers = await followedHeaders({
      Authorization: 'test-credential', Cookie: 'session=test',
      'X-Api^Key': 'test-key', 'X-Token|Secret': 'test-key',
      'X-Token': 'preserve', 'X-Good': 'yes',
    }, ['X-Api^Key', 'X-Token|Secret'])
    for (const name of ['authorization', 'cookie', 'x-api^key', 'x-token|secret']) {
      assert.equal(headers[name], undefined, `${name} must be stripped across origins`)
    }
    assert.equal(headers['x-token'], 'preserve')
    assert.equal(headers['x-good'], 'yes')
  },

  async 'redirect-array-headers'() {
    const headers = await followedHeaders([
      'Authorization', 'test-credential', 'Proxy-Authorization', 'test-credential',
      'Cookie', 'session=test', 'X-Api-Key', 'test-key', 'X-Good', 'yes',
    ], ['X-Api-Key'])
    for (const name of ['authorization', 'proxy-authorization', 'cookie', 'x-api-key']) {
      assert.equal(headers[name], undefined, `${name} must be stripped across origins`)
    }
    assert.equal(headers['x-good'], 'yes')
  },
}

try {
  assert.ok(Object.hasOwn(cases, process.argv[2]), 'unknown regression case')
  await cases[process.argv[2]]()
} finally {
  for (const session of sessions) session.destroy()
  for (const server of servers) {
    server.closeAllConnections?.()
    if (server.listening) await new Promise((resolve) => server.close(resolve))
  }
}
