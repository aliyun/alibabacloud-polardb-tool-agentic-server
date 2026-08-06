import { describe, expect, it, vi } from 'vitest'

import {
  LOCALE_STORAGE_KEY,
  persistLocale,
  resolveLocale,
} from './locale'

describe('resolveLocale', () => {
  it('prefers a supported persisted locale over browser languages', () => {
    const storage = { getItem: vi.fn(() => 'zh-CN') }

    expect(resolveLocale(storage, ['en-US'], 'en-US')).toBe('zh-CN')
    expect(storage.getItem).toHaveBeenCalledWith(LOCALE_STORAGE_KEY)
  })

  it('matches Chinese and English language families case-insensitively', () => {
    const storage = { getItem: vi.fn(() => null) }

    expect(resolveLocale(storage, ['ZH-tw'], 'en-US')).toBe('zh-CN')
    expect(resolveLocale(storage, ['fr-FR', 'EN-gb'], 'fr-FR')).toBe('en-US')
  })

  it('falls back to English for unsupported browser languages', () => {
    const storage = { getItem: vi.fn(() => 'not-a-locale') }

    expect(resolveLocale(storage, ['fr-FR'], 'de-DE')).toBe('en-US')
  })

  it('continues when reading storage throws', () => {
    const storage = {
      getItem: vi.fn(() => {
        throw new DOMException('denied', 'SecurityError')
      }),
    }

    expect(resolveLocale(storage, ['zh-HK'], 'en-US')).toBe('zh-CN')
  })

  it('does not write a browser-derived locale during resolution', () => {
    const storage = {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
    }

    expect(resolveLocale(storage, ['zh-CN'], 'zh-CN')).toBe('zh-CN')
    expect(storage.setItem).not.toHaveBeenCalled()
  })
})

describe('persistLocale', () => {
  it('stores an explicit canonical locale', () => {
    const storage = { setItem: vi.fn() }

    persistLocale(storage, 'zh-CN')

    expect(storage.setItem).toHaveBeenCalledWith(LOCALE_STORAGE_KEY, 'zh-CN')
  })

  it('does not throw when storage is unavailable', () => {
    const storage = {
      setItem: vi.fn(() => {
        throw new DOMException('denied', 'QuotaExceededError')
      }),
    }

    expect(() => persistLocale(storage, 'en-US')).not.toThrow()
  })
})
