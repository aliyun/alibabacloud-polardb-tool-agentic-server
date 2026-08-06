import { describe, expect, it } from 'vitest'

import { createTestI18n } from './i18n'
import { enUS } from './locales/en-US'
import { zhCN } from './locales/zh-CN'

function flattenKeys(value: unknown, prefix = ''): string[] {
  if (typeof value !== 'object' || value === null) return [prefix]
  return Object.entries(value).flatMap(([key, child]) =>
    flattenKeys(child, prefix ? `${prefix}.${key}` : key),
  )
}

describe('translation resources', () => {
  it('keeps Simplified Chinese keys identical to canonical English keys', () => {
    expect(flattenKeys(zhCN).sort()).toEqual(flattenKeys(enUS).sort())
  })

  it('interpolates translated values without escaping React content', () => {
    const instance = createTestI18n('zh-CN')

    expect(instance.t('common.itemCount', { count: 3 })).toBe('3 项')
  })
})
