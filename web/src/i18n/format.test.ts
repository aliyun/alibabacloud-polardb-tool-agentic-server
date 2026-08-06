import { describe, expect, it } from 'vitest'

import { formatDateTime, formatNumber } from './format'

describe('locale formatters', () => {
  it('formats numbers with the requested locale', () => {
    expect(formatNumber(1234567.89, 'en-US')).toBe('1,234,567.89')
  })

  it('formats dates with the requested locale and time zone', () => {
    const value = '2026-08-06T02:00:00Z'

    expect(formatDateTime(value, 'en-US', 'UTC')).toContain('Aug 6, 2026')
    expect(formatDateTime(value, 'zh-CN', 'UTC')).toContain('2026年8月6日')
  })
})
