import { Button, Dropdown } from 'antd'
import { GlobalOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

import {
  DEFAULT_LOCALE,
  isSupportedLocale,
  localeRegistry,
  persistLocale,
  type SupportedLocale,
} from '../../i18n/locale'
import './LanguageSwitcher.css'

export default function LanguageSwitcher() {
  const { t, i18n } = useTranslation()
  const active = isSupportedLocale(i18n.resolvedLanguage ?? i18n.language)
    ? (i18n.resolvedLanguage ?? i18n.language) as SupportedLocale
    : DEFAULT_LOCALE

  return (
    <Dropdown
      menu={{
        selectedKeys: [active],
        items: Object.entries(localeRegistry).map(([key, definition]) => ({
          key,
          label: definition.selfName,
        })),
        onClick: async ({ key }) => {
          if (!isSupportedLocale(key)) return
          await i18n.changeLanguage(key)
          persistLocale(typeof window === 'undefined' ? undefined : window.localStorage, key)
        },
      }}
      trigger={['click']}
    >
      <Button
        className="language-switcher"
        icon={<GlobalOutlined />}
        aria-label={t('common.switchLanguage')}
      >
        <span className="language-switcher-label">{localeRegistry[active].selfName}</span>
      </Button>
    </Dropdown>
  )
}
