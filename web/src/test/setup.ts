import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'
import { i18n } from '../i18n/i18n'

if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })

  const getComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = (element: Element) => getComputedStyle(element)
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(async () => {
  cleanup()
  await i18n.changeLanguage('en-US')
  if (typeof localStorage !== 'undefined') {
    localStorage.clear()
    sessionStorage.clear()
  }
  vi.restoreAllMocks()
})
