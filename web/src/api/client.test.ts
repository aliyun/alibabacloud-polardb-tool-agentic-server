import axios, { AxiosError, AxiosHeaders, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { expect, it, vi } from 'vitest'

import api from './client'

it('adds the browser configuration CSRF signal by default', () => {
  expect(api.defaults.headers['X-PAS-CSRF']).toBe('1')
})

function response(config: InternalAxiosRequestConfig, status = 200) {
  return { data: { ok: true }, status, statusText: String(status), headers: new AxiosHeaders(), config }
}

function unauthorized(config: InternalAxiosRequestConfig) {
  return new AxiosError('Unauthorized', AxiosError.ERR_BAD_REQUEST, config, undefined, response(config, 401))
}

async function withAdapters(adapter: AxiosAdapter, refresh: AxiosAdapter, action: () => Promise<void>) {
  const oldApi = api.defaults.adapter
  const oldAxios = axios.defaults.adapter
  api.defaults.adapter = adapter
  axios.defaults.adapter = refresh
  try {
    await action()
  } finally {
    api.defaults.adapter = oldApi
    axios.defaults.adapter = oldAxios
  }
}

it('preserves credentials and CSRF protection across a refresh and retry', async () => {
  const attempts: InternalAxiosRequestConfig[] = []
  const refresh = vi.fn<AxiosAdapter>(async (config) => {
    expect(config.url).toBe('/auth/refresh')
    expect(config.method).toBe('post')
    expect(config.withCredentials).toBe(true)
    return response(config)
  })
  await withAdapters(async (config) => {
    attempts.push(config)
    if (attempts.length === 1) throw unauthorized(config)
    return response(config)
  }, refresh, async () => {
    expect((await api.get('/api/agents')).status).toBe(200)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(attempts).toHaveLength(2)
    for (const config of attempts) {
      expect(config.url).toBe('/api/agents')
      expect(config.withCredentials).toBe(true)
      expect(config.headers.get('X-PAS-CSRF')).toBe('1')
    }
  })
})

it('shares one refresh across concurrent unauthorized requests', async () => {
  let completeRefresh!: () => void
  const gate = new Promise<void>((resolve) => { completeRefresh = resolve })
  const refresh = vi.fn<AxiosAdapter>(async (config) => {
    await gate
    return response(config)
  })
  const attempts = new Map<string, number>()
  await withAdapters(async (config) => {
    const url = config.url!
    const count = (attempts.get(url) ?? 0) + 1
    attempts.set(url, count)
    if (count === 1) throw unauthorized(config)
    return response(config)
  }, refresh, async () => {
    const pending = Promise.all([api.get('/api/agents'), api.get('/api/users')])
    try {
      await vi.waitFor(() => {
        expect(refresh).toHaveBeenCalledTimes(1)
        expect(attempts.size).toBe(2)
      })
    } finally {
      completeRefresh()
      await pending
    }
    expect(refresh).toHaveBeenCalledTimes(1)
    expect([...attempts.values()]).toEqual([2, 2])
  })
})

it('does not refresh requests that opt out of authentication redirects', async () => {
  const refresh = vi.fn<AxiosAdapter>()
  await withAdapters(async (config) => { throw unauthorized(config) }, refresh, async () => {
    await expect(api.get('/api/setup', { pasSkipAuthRedirect: true })).rejects.toBeInstanceOf(AxiosError)
    expect(refresh).not.toHaveBeenCalled()
  })
})
