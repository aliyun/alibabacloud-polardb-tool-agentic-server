// @vitest-environment node

import { expect, it } from 'vitest'

import config from './vite.config'

it('proxies setup discovery to the backend in local development', () => {
  if (typeof config === 'function') {
    throw new Error('Expected a static Vite configuration')
  }

  expect(config.server?.proxy).toMatchObject({
    '/readyz': {
      target: 'http://localhost:18760',
      changeOrigin: true,
    },
  })
})
