import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { createTestI18n } from '../../i18n/i18n'
import { LOCALE_STORAGE_KEY } from '../../i18n/locale'
import LocaleProvider from '../../i18n/LocaleProvider'
import LanguageSwitcher from '.'

describe('LanguageSwitcher', () => {
  it('switches immediately and persists only the explicit selection', async () => {
    const user = userEvent.setup()
    const instance = createTestI18n('zh-CN')

    render(
      <LocaleProvider i18nInstance={instance}>
        <LanguageSwitcher />
        <p>{instance.t('auth.welcomeTitle')}</p>
      </LocaleProvider>,
    )

    expect(localStorage).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: '切换语言' }))
    await user.click(await screen.findByText('English'))

    expect(instance.language).toBe('en-US')
    expect(document.documentElement.lang).toBe('en-US')
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('en-US')
  })
})
