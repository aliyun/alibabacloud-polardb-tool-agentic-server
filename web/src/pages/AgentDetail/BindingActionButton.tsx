import { DownOutlined } from '@ant-design/icons'
import { Button, Dropdown } from 'antd'
import { useTranslation } from 'react-i18next'

export default function BindingActionButton({
  canBind, canUnbind, disabled, loading, bindLabel, unbindLabel, onBind, onUnbind,
}: {
  canBind: boolean
  canUnbind: boolean
  disabled?: boolean
  loading?: boolean
  bindLabel: string
  unbindLabel: string
  onBind: () => void
  onUnbind: () => void
}) {
  const { t } = useTranslation()
  const blocked = disabled || loading || (!canBind && !canUnbind)
  if (canBind && canUnbind) {
    return (
      <Dropdown.Button
        type="primary"
        trigger={['click']}
        icon={<DownOutlined aria-label={t('agentKnowledgeWorkflow.moreBindingActions')} />}
        disabled={blocked}
        loading={loading}
        onClick={() => { if (!blocked) onBind() }}
        menu={{
          items: [{ key: 'unbind', label: unbindLabel, danger: true }],
          onClick: () => { if (!blocked) onUnbind() },
        }}
      >
        {bindLabel}
      </Dropdown.Button>
    )
  }
  const unbind = !canBind && canUnbind
  return (
    <Button
      type={unbind ? 'default' : 'primary'}
      danger={unbind}
      disabled={blocked}
      loading={loading}
      onClick={() => { if (!blocked) (unbind ? onUnbind : onBind)() }}
    >
      {unbind ? unbindLabel : bindLabel}
    </Button>
  )
}
