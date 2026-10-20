import { Alert, Button, Select, Space, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import type { EndpointCatalog, OpenAPINetwork } from '../../api/aliyunEndpoints'
import { useEndpointCatalog, regionAvailable } from './useEndpointCatalog'


interface Props {
  id?: string
  value?: string
  onChange?: (value: string | undefined) => void
  disabled?: boolean
  allowClear?: boolean
  catalog?: EndpointCatalog
  network?: OpenAPINetwork
  accessRegion?: string
  requireSTS?: boolean
  resource?: boolean
  'aria-label'?: string
}

export default function RegionSelect({
  catalog: supplied, network, accessRegion, requireSTS = false,
  resource = false, ...props
}: Props) {
  const { t, i18n } = useTranslation()
  const { catalog, failed, retry } = useEndpointCatalog(supplied)
  const effectiveNetwork = network ?? (resource ? catalog?.defaults?.openapi_network : undefined)
  const effectiveAccessRegion = accessRegion ?? (resource ? catalog?.defaults?.region_id : undefined)
  const entry = catalog?.regions.find((item) => item.region_id === props.value)
  const supported = regionAvailable(catalog, props.value, effectiveNetwork ?? 'public', requireSTS, effectiveAccessRegion)
  const options = (catalog?.regions ?? []).map((item) => ({
    value: item.region_id,
    label: `${item.label[i18n.language === 'zh-CN' ? 'zh-CN' : 'en']} · ${item.region_id}`,
    disabled: !regionAvailable(catalog, item.region_id, effectiveNetwork ?? 'public', requireSTS, effectiveAccessRegion),
  }))
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Select {...props} style={{ width: '100%' }} showSearch optionFilterProp="label"
        placeholder={t('cloudConnection.chooseRegion')} options={options}
        loading={!catalog && !failed} disabled={props.disabled || !catalog} />
      {failed && <Alert type="error" showIcon message={t('cloudConnection.catalogFailed')}
        action={<Button size="small" onClick={retry}>{t('cloudConnection.retry')}</Button>} />}
      {props.value && catalog && !supported && <Alert type="warning" showIcon message={t('cloudConnection.unavailable')} />}
      {resource && effectiveNetwork === 'vpc' && <Typography.Text type="secondary">{t('cloudConnection.vpcConstraint', { region: effectiveAccessRegion })}</Typography.Text>}
      {entry && supported && effectiveNetwork && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>
        {t('cloudConnection.endpointPreview', { service: 'PolarDB', network: effectiveNetwork, endpoint: entry.polardb[effectiveNetwork] })}
      </Typography.Text>}
      {entry && requireSTS && supported && effectiveNetwork && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>
        {t('cloudConnection.endpointPreview', { service: 'STS', network: effectiveNetwork, endpoint: entry.sts[effectiveNetwork] })}
      </Typography.Text>}
    </Space>
  )
}
