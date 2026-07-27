import { expect, it, vi } from 'vitest'

import api from './client'
import { discoverSystemState } from './configuration'

it('rejects a readiness response without a valid setup mode', async () => {
  vi.spyOn(api, 'get').mockResolvedValue({
    data: '<html>Vite fallback</html>',
  })

  await expect(discoverSystemState()).rejects.toThrow(
    'Invalid server readiness response',
  )
})
