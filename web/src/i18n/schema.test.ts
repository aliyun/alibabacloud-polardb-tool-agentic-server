import { describe, expect, it } from 'vitest'

import { createTestI18n } from './i18n'
import { schemaFieldLabel } from './schema'

describe('schemaFieldLabel', () => {
  it('translates known stable module and field names', () => {
    const i18n = createTestI18n('zh-CN')

    expect(
      schemaFieldLabel(i18n.t, 'core_admin', 'password', 'Administrator password'),
    ).toBe('管理员密码')
    expect(
      schemaFieldLabel(i18n.t, 'settings', 'pool_target_size', 'Pool target size'),
    ).toBe('资源池目标大小')
  })

  it('preserves the backend title for unknown fields', () => {
    const i18n = createTestI18n('zh-CN')

    expect(
      schemaFieldLabel(i18n.t, 'future_module', 'future_field', 'Future field'),
    ).toBe('Future field')
  })
})
