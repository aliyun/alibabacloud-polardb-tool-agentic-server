import { render, screen } from '@testing-library/react'
import { Pagination } from 'antd'
import { describe, expect, it } from 'vitest'

import { createTestI18n } from './i18n'
import LocaleProvider from './LocaleProvider'

describe('LocaleProvider', () => {
  it('synchronizes the canonical HTML language and Ant Design locale', () => {
    const instance = createTestI18n('zh-CN')

    render(
      <LocaleProvider i18nInstance={instance}>
        <Pagination current={2} total={100} />
      </LocaleProvider>,
    )

    expect(document.documentElement.lang).toBe('zh-CN')
    expect(screen.getByTitle('上一页')).toBeInTheDocument()
    expect(localStorage).toHaveLength(0)
  })
})
