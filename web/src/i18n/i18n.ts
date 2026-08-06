import i18next, { createInstance, type i18n as I18nInstance } from 'i18next'
import { initReactI18next } from 'react-i18next'

import {
  DEFAULT_LOCALE,
  localeRegistry,
  resolveLocale,
  type SupportedLocale,
} from './locale'

const resources = {
  'en-US': { translation: localeRegistry['en-US'].resources },
  'zh-CN': { translation: localeRegistry['zh-CN'].resources },
}

function initialize(
  instance: I18nInstance,
  locale: SupportedLocale,
  registerReactPlugin: boolean,
): I18nInstance {
  if (registerReactPlugin) instance.use(initReactI18next)
  void instance.init({
    resources,
    lng: locale,
    fallbackLng: DEFAULT_LOCALE,
    supportedLngs: Object.keys(localeRegistry),
    interpolation: { escapeValue: false },
    initAsync: false,
  })
  return instance
}

function browserLocale(): SupportedLocale {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') return DEFAULT_LOCALE
  return resolveLocale(window.localStorage, navigator.languages, navigator.language)
}

export const i18n = initialize(i18next, browserLocale(), true)

export function createTestI18n(locale: SupportedLocale = DEFAULT_LOCALE): I18nInstance {
  return initialize(createInstance(), locale, false)
}
