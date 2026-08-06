import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { createTestI18n } from '../../i18n/i18n'
import LocaleProvider from '../../i18n/LocaleProvider'
import Login from '.'

describe('Login localization', () => {
  it('renders Chinese application copy while preserving the product name', () => {
    render(
      <LocaleProvider i18nInstance={createTestI18n('zh-CN')}>
        <MemoryRouter>
          <Login onLogin={vi.fn()} />
        </MemoryRouter>
      </LocaleProvider>,
    )

    expect(screen.getByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /登\s*录/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '切换语言' })).toBeInTheDocument()
    expect(screen.getByRole('heading', {
      name: 'alibabacloud polardb tool agentic server',
    })).toBeInTheDocument()
  })
})
