import { describe, expect, it, vi } from 'vitest'

import { copyText } from './clipboard'

describe('copyText', () => {
  it('falls back to execCommand when Clipboard API is unavailable', async () => {
    const clipboardDescriptor = Object.getOwnPropertyDescriptor(
      navigator,
      'clipboard',
    )
    const execCommandDescriptor = Object.getOwnPropertyDescriptor(
      document,
      'execCommand',
    )
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    })
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    })

    try {
      await copyText('pas_agent_secret')
      expect(execCommand).toHaveBeenCalledWith('copy')
      expect(document.querySelector('textarea')).toBeNull()
    } finally {
      if (clipboardDescriptor) {
        Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
      } else {
        delete (navigator as { clipboard?: Clipboard }).clipboard
      }
      if (execCommandDescriptor) {
        Object.defineProperty(document, 'execCommand', execCommandDescriptor)
      } else {
        delete (document as { execCommand?: typeof document.execCommand })
          .execCommand
      }
    }
  })
})
