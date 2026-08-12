import { Alert, Button, Checkbox, Form, Radio, Space, Typography } from 'antd'
import type { FormEvent } from 'react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type {
  AliyunAccessMutation,
  AliyunCredentialMode,
  ConfigModule,
  SecretMarker,
} from '../../api/configuration'
import './AliyunAccessForm.css'
import { ModeFields } from './ModeFields'
import { RetainedSummary } from './RetainedSummary'
import { useAliyunAccessFormState } from './useAliyunAccessFormState'

interface Props {
  module: ConfigModule
  disabled?: boolean
  onSubmit: (values: AliyunAccessMutation) => void | Promise<void>
  onValuesChange?: () => void
  formId?: string
}

const modes: AliyunCredentialMode[] = ['direct_ak', 'assume_role', 'ecs_ram_role']

function isMarker(value: unknown): value is SecretMarker {
  return typeof value === 'object' && value !== null && (value as SecretMarker).configured === true
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : {}
}

function markerText(value: unknown): string | undefined {
  return isMarker(value) ? value.display_hint : undefined
}

function completeMarkerPair(block: Record<string, unknown>, id: string, secret: string): boolean {
  return isMarker(block[id]) && isMarker(block[secret])
}

export default function AliyunAccessForm({
  module,
  disabled = false,
  onSubmit,
  onValuesChange,
  formId = `config-form-${module.name}`,
}: Props) {
  const { t, i18n } = useTranslation()
  const { name: moduleName, revision: moduleRevision, draft: moduleDraft, effective: moduleEffective } = module
  const snapshotId = `${moduleName}:${moduleRevision}`
  const snapshot = useMemo(() => {
    const snapshotConfig = { ...(moduleEffective?.config ?? {}), ...(moduleDraft ?? {}) }
    const snapshotMode = modes.includes(snapshotConfig.credential_mode as AliyunCredentialMode)
      ? snapshotConfig.credential_mode as AliyunCredentialMode
      : 'direct_ak'
    return { config: snapshotConfig, mode: snapshotMode }
  }, [moduleDraft, moduleEffective])
  const { config, mode: currentMode } = snapshot
  const [mode, setMode] = useState<AliyunCredentialMode>(currentMode)
  const [previousAction, setPreviousAction] = useState<'clear' | 'retain'>('clear')
  const [selectedAction, setSelectedAction] = useState<'replace' | 'reuse_retained' | undefined>('replace')
  const [reuseDirectSource, setReuseDirectSource] = useState(false)
  const [reuseConfirmed, setReuseConfirmed] = useState(false)
  const [replaceActive, setReplaceActive] = useState(false)
  const [replaceExternal, setReplaceExternal] = useState(false)
  const [deleteMode, setDeleteMode] = useState<AliyunCredentialMode | null>(null)
  const [deleteConfirmed, setDeleteConfirmed] = useState(false)

  const { values, dirtyFields, changeValue, resetModeState, buildActiveMutation, buildTransition } = useAliyunAccessFormState(module, config, onValuesChange)
  const reuseErrorRef = useRef<HTMLDivElement>(null)
  const [reuseError, setReuseError] = useState<'choice' | 'confirmation' | null>(null)

  useEffect(() => {
    setMode(snapshot.mode); setPreviousAction('clear'); setSelectedAction('replace')
    setReuseDirectSource(false); setReuseConfirmed(false); setReplaceActive(false); setReplaceExternal(false)
    setDeleteMode(null); setDeleteConfirmed(false); setReuseError(null)
  }, [snapshot, snapshotId])

  const selectedBlock = asRecord(config[mode])
  const currentBlock = asRecord(config[currentMode])
  const targetIsRetained = mode !== currentMode && Boolean(config[mode])
  const retainedModes = modes.filter(
    (item) => item !== currentMode && item !== mode && Boolean(config[item]),
  )
  const directMarker = markerText(selectedBlock.access_key_id)
  const assumeMarker = markerText(selectedBlock.source_access_key_id)
  const hasStoredDirect = completeMarkerPair(selectedBlock, 'access_key_id', 'access_key_secret')
  const hasStoredAssumeSource = completeMarkerPair(selectedBlock, 'source_access_key_id', 'source_access_key_secret')
  const currentDirectBlock = asRecord(config.direct_ak)
  const canReuseCurrentDirect = completeMarkerPair(currentDirectBlock, 'access_key_id', 'access_key_secret')
  const canReuseSelected = mode === 'ecs_ram_role' || (mode === 'direct_ak' ? hasStoredDirect : hasStoredAssumeSource)
  const modeChanged = mode !== currentMode
  const hasCurrentCredential = Boolean(config[currentMode])
  const usesCurrentDirectAsSource = currentMode === 'direct_ak' && mode === 'assume_role' && reuseDirectSource
  const effectiveSelectedAction = !modeChanged && dirtyFields.size === 0 && !replaceActive && !replaceExternal && Boolean(config[mode])
    ? 'reuse_retained'
    : selectedAction

  const selectMode = (nextMode: AliyunCredentialMode) => {
    setMode(nextMode)
    setSelectedAction(nextMode !== currentMode && Boolean(config[nextMode]) ? undefined : 'replace')
    setPreviousAction('clear')
    setReuseDirectSource(false)
    setReuseConfirmed(false)
    setReplaceActive(false)
    setReplaceExternal(false)
    setDeleteMode(null)
    setDeleteConfirmed(false)
    resetModeState(); setReuseError(null)
    onValuesChange?.()
  }

  const credentialTransition = () => buildTransition({
    mode, currentMode, previousAction, selectedAction: effectiveSelectedAction,
    reuseDirectSource: usesCurrentDirectAsSource, reuseConfirmed,
  })

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (disabled) return
    if (targetIsRetained && selectedAction === undefined) {
      showReuseError('choice'); onValuesChange?.(); return
    }
    if (usesCurrentDirectAsSource && !canReuseCurrentDirect) {
      showReuseError('choice'); return
    }
    if (usesCurrentDirectAsSource && !reuseConfirmed) {
      showReuseError('confirmation'); return
    }
    onSubmit({ ...buildActiveMutation({
      mode, currentMode, selectedAction: effectiveSelectedAction, replaceActive, replaceExternal,
      usesCurrentDirectAsSource, hasStoredAssumeSource, hasStoredExternalId: isMarker(selectedBlock.external_id),
    }), transition: credentialTransition() })
  }

  const deleteRetained = () => {
    if (!deleteMode || !deleteConfirmed) return
    onSubmit({
      credential_mode: currentMode,
      transition: buildTransition({
        mode: currentMode, currentMode, previousAction: 'clear', selectedAction: 'reuse_retained',
        reuseDirectSource: false, reuseConfirmed: false, deleteRetainedModes: [deleteMode],
      }),
    })
  }

  const choosePreviousAction = (action: 'clear' | 'retain') => {
    setPreviousAction(action); onValuesChange?.()
  }

  const chooseRetainedAction = (action: 'replace' | 'reuse_retained') => {
    setSelectedAction(action); setReuseDirectSource(false); setReuseConfirmed(false)
    setDeleteMode(null); setDeleteConfirmed(false); setReuseError(null); onValuesChange?.()
  }

  const showReplacement = (external = false) => {
    if (external) setReplaceExternal(true)
    else setReplaceActive(true)
    onValuesChange?.()
  }

  const chooseDirectSource = (reuse: boolean) => {
    setReuseDirectSource(reuse); setReuseConfirmed(false); onValuesChange?.()
  }

  const confirmDirectSource = (confirmed: boolean) => {
    setReuseConfirmed(confirmed); setReuseError(null); onValuesChange?.()
  }

  const showReuseError = (error: 'choice' | 'confirmation') => {
    if (reuseError === error) reuseErrorRef.current?.focus()
    setReuseError(error)
  }

  useEffect(() => { if (reuseError) reuseErrorRef.current?.focus() }, [reuseError])

  const secretMarker = (value: unknown, accessKeyMarker?: string) => {
    if (!isMarker(value) && !accessKeyMarker) return null
    return (
      <div className="aliyun-access-form__marker" role="status">
        <Typography.Text>{accessKeyMarker ?? t('components.aliyunAccess.secretConfigured')}</Typography.Text>
        {isMarker(value) && value.updated_at && (
          <Typography.Text type="secondary">{t('components.aliyunAccess.updatedAt', { value: new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value.updated_at)) })}</Typography.Text>
        )}
      </div>
    )
  }

  return (
    <form id={formId} className="aliyun-access-form" onSubmit={submit} aria-label={t('components.aliyunAccess.formLabel')}>
      <Alert
        className="aliyun-access-form__intro"
        type="info"
        showIcon
        message={t('components.aliyunAccess.intro')}
        description={
          <Space size={12} wrap>
            <Typography.Link href={i18n.language === 'zh-CN' ? 'https://help.aliyun.com/zh/ram/product-overview/quick-start-create-and-use-accesskey-pairs-for-programmatic-calls' : 'https://www.alibabacloud.com/help/en/ram/user-guide/create-an-accesskey-pair'} target="_blank" rel="noopener noreferrer">{t('components.aliyunAccess.accessKeyDocs')}</Typography.Link>
            <Typography.Link href={i18n.language === 'zh-CN' ? 'https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-assumerole' : 'https://www.alibabacloud.com/help/en/ram/developer-reference/api-sts-2015-04-01-assumerole'} target="_blank" rel="noopener noreferrer">{t('components.aliyunAccess.assumeRoleDocs')}</Typography.Link>
            <Typography.Link href={i18n.language === 'zh-CN' ? 'https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance' : 'https://www.alibabacloud.com/help/en/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance'} target="_blank" rel="noopener noreferrer">{t('components.aliyunAccess.ecsRamRoleDocs')}</Typography.Link>
          </Space>
        }
      />

      <Form.Item label={t('components.aliyunAccess.modeLabel')}>
        <Radio.Group value={mode} onChange={(event) => selectMode(event.target.value)} disabled={disabled} className="aliyun-access-form__modes">
          {modes.map((item) => (
            <Radio key={item} value={item} className="aliyun-access-form__mode">
              <span className="aliyun-access-form__mode-copy">
                <Typography.Text strong>{t(`components.aliyunAccess.modes.${item}.title`)}</Typography.Text>
                <Typography.Text type="secondary">{t(`components.aliyunAccess.modes.${item}.description`)}</Typography.Text>
              </span>
            </Radio>
          ))}
        </Radio.Group>
      </Form.Item>

      {modeChanged && hasCurrentCredential && (
        <Alert
          className="aliyun-access-form__transition"
          type="warning"
          showIcon
          message={t('components.aliyunAccess.switchTitle', { mode: t(`components.aliyunAccess.modes.${currentMode}.title`) })}
          description={
            <Space direction="vertical" size={8}>
              <RetainedSummary mode={currentMode} block={currentBlock} locale={i18n.language} t={t} />
              <Radio.Group value={previousAction} onChange={(event) => choosePreviousAction(event.target.value)} disabled={disabled}>
                <Space direction="vertical">
                  <Radio value="clear">{t('components.aliyunAccess.clearPrevious')}</Radio>
                  <Radio value="retain">{t('components.aliyunAccess.retainPrevious')}</Radio>
                </Space>
              </Radio.Group>
            </Space>
          }
        />
      )}

      {targetIsRetained && (
        <Form.Item label={t('components.aliyunAccess.retainedChoiceLabel')}>
          <Radio.Group value={selectedAction} onChange={(event) => chooseRetainedAction(event.target.value)} disabled={disabled}>
            <Space direction="vertical">
              <Radio value="reuse_retained" disabled={!canReuseSelected}>{t('components.aliyunAccess.useRetained')}</Radio>
              <Radio value="replace">{t('components.aliyunAccess.enterNew')}</Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
      )}
      {targetIsRetained && <Alert type="info" showIcon message={t('components.aliyunAccess.retainedSelectedSummary', { mode: t(`components.aliyunAccess.modes.${mode}.title`) })} description={<RetainedSummary mode={mode} block={selectedBlock} locale={i18n.language} t={t} />} />}
      {reuseError && <div ref={reuseErrorRef} tabIndex={-1}><Alert type="error" showIcon message={t(reuseError === 'confirmation' ? 'components.aliyunAccess.confirmationRequired' : 'components.aliyunAccess.choiceRequired')} /></div>}

      <ModeFields mode={mode} currentMode={currentMode} selectedAction={selectedAction} disabled={disabled} values={values} selectedBlock={selectedBlock} directMarker={directMarker} assumeMarker={assumeMarker} hasStoredDirect={hasStoredDirect} hasStoredAssumeSource={hasStoredAssumeSource} canReuseCurrentDirect={canReuseCurrentDirect} usesCurrentDirectAsSource={usesCurrentDirectAsSource} reuseDirectSource={reuseDirectSource} reuseConfirmed={reuseConfirmed} replaceActive={replaceActive} replaceExternal={replaceExternal} t={t} secretMarker={secretMarker} changeValue={changeValue} showReplacement={showReplacement} chooseDirectSource={chooseDirectSource} confirmDirectSource={confirmDirectSource} />

      {retainedModes.length > 0 && (
        <Alert className="aliyun-access-form__retained" type="info" showIcon message={t('components.aliyunAccess.retainedTitle')} description={retainedModes.map((retainedMode) => {
          const block = asRecord(config[retainedMode])
          return <div key={retainedMode}><Space wrap><Typography.Text>{t(`components.aliyunAccess.modes.${retainedMode}.title`)}</Typography.Text><RetainedSummary mode={retainedMode} block={block} locale={i18n.language} t={t} /><Button type="link" danger aria-label={t('components.aliyunAccess.deleteRetainedMode', { mode: t(`components.aliyunAccess.modes.${retainedMode}.title`) })} onClick={() => { setDeleteMode(retainedMode); setDeleteConfirmed(false); onValuesChange?.() }} disabled={disabled}>{t('components.aliyunAccess.deleteRetained')}</Button></Space></div>
        })} />
      )}

      {deleteMode && (
        <Alert className="aliyun-access-form__delete-confirm" type="error" showIcon message={t('components.aliyunAccess.deleteConfirmTitle')} description={<Space direction="vertical"><Checkbox checked={deleteConfirmed} onChange={(event) => { setDeleteConfirmed(event.target.checked); onValuesChange?.() }} disabled={disabled}>{t('components.aliyunAccess.confirmDelete', { mode: t(`components.aliyunAccess.modes.${deleteMode}.title`) })}</Checkbox><Space><Button danger onClick={deleteRetained} disabled={disabled || !deleteConfirmed}>{t('components.aliyunAccess.confirmDeletion')}</Button><Button onClick={() => { setDeleteMode(null); onValuesChange?.() }} disabled={disabled}>{t('common.cancel')}</Button></Space></Space>} />
      )}
    </form>
  )
}
