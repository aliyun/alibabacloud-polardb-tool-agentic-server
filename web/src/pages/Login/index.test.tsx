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
    expect(screen.getByRole('button', { name: /^登\s*录$/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '使用飞书登录' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '使用 SharePoint 登录' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '切换语言' })).toBeInTheDocument()
    expect(screen.getByRole('heading', {
      name: 'alibabacloud polardb tool agentic server',
    })).toBeInTheDocument()
  })

  it('shows only the enterprise SSO action in OIDC mode', () => {
    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <MemoryRouter>
          <Login
            onLogin={vi.fn()}
            authModeInfo={{
              mode: 'oidc',
              provider_name: 'Example Identity',
              sso_login_url: '/auth/oidc/login',
              recovery_login_path: '/login/recovery',
            }}
          />
        </MemoryRouter>
      </LocaleProvider>,
    )

    expect(screen.getByRole('link', {
      name: 'Continue with Example Identity',
    })).toHaveAttribute('href', '/auth/oidc/login')
    expect(screen.queryByPlaceholderText('Username')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in with Feishu' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Administrator recovery' }))
      .toHaveAttribute('href', '/login/recovery')
  })

  it('keeps the built-in form on the recovery route', () => {
    render(
      <LocaleProvider i18nInstance={createTestI18n('en-US')}>
        <MemoryRouter>
          <Login
            onLogin={vi.fn()}
            authModeInfo={{
              mode: 'oidc',
              provider_name: 'Example Identity',
              sso_login_url: '/auth/oidc/login',
              recovery_login_path: '/login/recovery',
            }}
            recovery
          />
        </MemoryRouter>
      </LocaleProvider>,
    )

    expect(screen.getByRole('heading', {
      name: 'Administrator recovery',
    })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Username')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign In' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in with Feishu' })).not.toBeInTheDocument()
  })
})
