import { useEffect, useState, type ReactNode } from 'react'
import { App as AntApp, ConfigProvider } from 'antd'
import type { i18n as I18nInstance } from 'i18next'
import { I18nextProvider } from 'react-i18next'

import { appTheme } from '../styles/theme'
import { i18n as applicationI18n } from './i18n'
import {
  DEFAULT_LOCALE,
  isSupportedLocale,
  localeRegistry,
  type SupportedLocale,
} from './locale'

interface LocaleProviderProps {
  children: ReactNode
  i18nInstance?: I18nInstance
}

function activeLocale(instance: I18nInstance): SupportedLocale {
  const language = instance.resolvedLanguage ?? instance.language
  return isSupportedLocale(language) ? language : DEFAULT_LOCALE
}

export default function LocaleProvider({
  children,
  i18nInstance = applicationI18n,
}: LocaleProviderProps) {
  const [locale, setLocale] = useState<SupportedLocale>(() => activeLocale(i18nInstance))

  useEffect(() => {
    const handleLanguageChanged = () => setLocale(activeLocale(i18nInstance))
    i18nInstance.on('languageChanged', handleLanguageChanged)
    handleLanguageChanged()
    return () => {
      i18nInstance.off('languageChanged', handleLanguageChanged)
    }
  }, [i18nInstance])

  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])

  return (
    <I18nextProvider i18n={i18nInstance}>
      <ConfigProvider theme={appTheme} locale={localeRegistry[locale].antdLocale}>
        <AntApp>{children}</AntApp>
      </ConfigProvider>
    </I18nextProvider>
  )
}
