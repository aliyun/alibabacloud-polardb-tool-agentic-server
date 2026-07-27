import { expect, it } from 'vitest'

import api from './client'

it('adds the browser configuration CSRF signal by default', () => {
  expect(api.defaults.headers['X-PAS-CSRF']).toBe('1')
})
