import { useTranslation } from 'react-i18next'

import PageContainer from '../../components/PageContainer'
import DedicatedPoolPanel from './DedicatedPoolPanel'

export default function Pool() {
  const { t } = useTranslation()

  return (
    <PageContainer
      title={t('pool.title')}
      description={t('pool.description')}
    >
      <DedicatedPoolPanel />
    </PageContainer>
  )
}
