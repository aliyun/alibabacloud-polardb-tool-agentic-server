import type { Locale } from 'antd/es/locale'
import enUSAntd from 'antd/locale/en_US'
import zhCNAntd from 'antd/locale/zh_CN'

import { enUS } from './locales/en-US'
import { zhCN } from './locales/zh-CN'

export type SupportedLocale = 'en-US' | 'zh-CN'

export const LOCALE_STORAGE_KEY = 'pas.ui.locale'
export const DEFAULT_LOCALE: SupportedLocale = 'en-US'

interface ReadableStorage {
  getItem(key: string): string | null
}

interface WritableStorage {
  setItem(key: string, value: string): void
}

interface LocaleDefinition {
  selfName: string
  antdLocale: Locale
  resources: typeof enUS | typeof zhCN
}

export const localeRegistry: Record<SupportedLocale, LocaleDefinition> = {
  'en-US': {
    selfName: 'English',
    antdLocale: enUSAntd,
    resources: enUS,
  },
  'zh-CN': {
    selfName: '简体中文',
    antdLocale: zhCNAntd,
    resources: zhCN,
  },
}

function normalizeLocale(value: string | null | undefined): SupportedLocale | undefined {
  const language = value?.trim().toLowerCase()
  if (!language) return undefined
  if (language === 'zh-cn' || language.startsWith('zh-') || language === 'zh') return 'zh-CN'
  if (language === 'en-us' || language.startsWith('en-') || language === 'en') return 'en-US'
  return undefined
}

export function readStoredLocale(storage?: ReadableStorage): SupportedLocale | undefined {
  try {
    return normalizeLocale(storage?.getItem(LOCALE_STORAGE_KEY))
  } catch {
    return undefined
  }
}

export function resolveLocale(
  storage: ReadableStorage | undefined,
  languages: readonly string[] | undefined,
  language?: string,
): SupportedLocale {
  const stored = readStoredLocale(storage)
  if (stored) return stored

  for (const candidate of [...(languages ?? []), language]) {
    const locale = normalizeLocale(candidate)
    if (locale) return locale
  }
  return DEFAULT_LOCALE
}

export function persistLocale(storage: WritableStorage | undefined, locale: SupportedLocale): void {
  try {
    storage?.setItem(LOCALE_STORAGE_KEY, locale)
  } catch {
    // The in-memory locale remains active when browser storage is unavailable.
  }
}

export function isSupportedLocale(value: string): value is SupportedLocale {
  return value in localeRegistry
}
