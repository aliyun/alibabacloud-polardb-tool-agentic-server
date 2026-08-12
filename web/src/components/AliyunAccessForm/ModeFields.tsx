import { Button, Checkbox, Collapse, Form, Input, InputNumber, Radio, Space, Tag, Typography } from 'antd'
import type { ReactNode } from 'react'
import type { TFunction } from 'i18next'

import type { AliyunCredentialMode } from '../../api/configuration'
import type { AliyunFormValues } from './useAliyunAccessFormState'

interface Props {
  mode: AliyunCredentialMode
  currentMode: AliyunCredentialMode
  selectedAction: 'replace' | 'reuse_retained' | undefined
  disabled: boolean
  values: AliyunFormValues
  selectedBlock: Record<string, unknown>
  directMarker?: string
  assumeMarker?: string
  hasStoredDirect: boolean
  hasStoredAssumeSource: boolean
  canReuseCurrentDirect: boolean
  usesCurrentDirectAsSource: boolean
  reuseDirectSource: boolean
  reuseConfirmed: boolean
  replaceActive: boolean
  replaceExternal: boolean
  t: TFunction
  secretMarker: (value: unknown, accessKeyMarker?: string) => ReactNode
  changeValue: (name: keyof AliyunFormValues, value: string | number) => void
  showReplacement: (external?: boolean) => void
  chooseDirectSource: (reuse: boolean) => void
  confirmDirectSource: (confirmed: boolean) => void
}

export function ModeFields({
  mode, currentMode, selectedAction, disabled, values, selectedBlock, directMarker, assumeMarker,
  hasStoredDirect, hasStoredAssumeSource, canReuseCurrentDirect, usesCurrentDirectAsSource,
  reuseDirectSource, reuseConfirmed, replaceActive, replaceExternal, t, secretMarker,
  changeValue, showReplacement, chooseDirectSource, confirmDirectSource,
}: Props) {
  if (selectedAction !== 'replace') return null

  if (mode === 'direct_ak') {
    return !replaceActive && hasStoredDirect ? (
      <Space direction="vertical" style={{ width: '100%' }}>
        {secretMarker(selectedBlock.access_key_id, directMarker)}
        {secretMarker(selectedBlock.access_key_secret)}
        <Button onClick={() => showReplacement()} disabled={disabled}>{t('components.aliyunAccess.replaceAccessKey')}</Button>
      </Space>
    ) : (
      <>
        <Form.Item label={t('components.aliyunAccess.accessKeyIdLabel')} required>
          <Input aria-label={t('components.aliyunAccess.accessKeyIdLabel')} value={values.accessKeyId} onChange={(event) => changeValue('accessKeyId', event.target.value)} autoComplete="off" required disabled={disabled} />
        </Form.Item>
        <Form.Item label={t('components.aliyunAccess.accessKeySecretLabel')} required>
          <Input.Password aria-label={t('components.aliyunAccess.accessKeySecretLabel')} value={values.accessKeySecret} onChange={(event) => changeValue('accessKeySecret', event.target.value)} autoComplete="new-password" required disabled={disabled} />
        </Form.Item>
      </>
    )
  }

  if (mode === 'assume_role') {
    return (
      <>
        {currentMode === 'direct_ak' && (
          <Form.Item>
            <Radio.Group value={reuseDirectSource ? 'reuse' : 'new'} onChange={(event) => chooseDirectSource(event.target.value === 'reuse')} disabled={disabled}>
              <Space direction="vertical">
                <Radio value="reuse" disabled={!canReuseCurrentDirect}>{t('components.aliyunAccess.useRetainedDirect')}</Radio>
                <Radio value="new">{t('components.aliyunAccess.enterNewSource')}</Radio>
              </Space>
            </Radio.Group>
            {reuseDirectSource && <Checkbox checked={reuseConfirmed} onChange={(event) => confirmDirectSource(event.target.checked)} disabled={disabled}>{t('components.aliyunAccess.confirmReuseDirect')}</Checkbox>}
          </Form.Item>
        )}
        {!usesCurrentDirectAsSource && (hasStoredAssumeSource && !replaceActive ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            {secretMarker(selectedBlock.source_access_key_id, assumeMarker)}
            {secretMarker(selectedBlock.source_access_key_secret)}
            <Button onClick={() => showReplacement()} disabled={disabled}>{t('components.aliyunAccess.replaceSourceAccessKey')}</Button>
          </Space>
        ) : (
          <>
            <Form.Item label={t('components.aliyunAccess.sourceAccessKeyIdLabel')} required><Input aria-label={t('components.aliyunAccess.sourceAccessKeyIdLabel')} value={values.sourceAccessKeyId} onChange={(event) => changeValue('sourceAccessKeyId', event.target.value)} required disabled={disabled} /></Form.Item>
            <Form.Item label={t('components.aliyunAccess.sourceAccessKeySecretLabel')} required><Input.Password aria-label={t('components.aliyunAccess.sourceAccessKeySecretLabel')} value={values.sourceAccessKeySecret} onChange={(event) => changeValue('sourceAccessKeySecret', event.target.value)} autoComplete="new-password" required disabled={disabled} /></Form.Item>
          </>
        ))}
        <Form.Item label={t('components.aliyunAccess.roleArnLabel')} required><Input aria-label={t('components.aliyunAccess.roleArnLabel')} value={values.roleArn} onChange={(event) => changeValue('roleArn', event.target.value)} required disabled={disabled} /></Form.Item>
        <Collapse className="aliyun-access-form__advanced" items={[{ key: 'advanced', label: t('components.aliyunAccess.advancedLabel'), children: <Space direction="vertical" style={{ width: '100%' }}><Form.Item label={t('components.aliyunAccess.roleSessionNameLabel')}><Input id="aliyun-role-session-name" aria-label={t('components.aliyunAccess.roleSessionNameLabel')} value={values.roleSessionName} onChange={(event) => changeValue('roleSessionName', event.target.value)} disabled={disabled} /></Form.Item><Form.Item label={t('components.aliyunAccess.durationLabel')}><InputNumber id="aliyun-duration" aria-label={t('components.aliyunAccess.durationLabel')} min={900} max={43200} value={values.durationSeconds} onChange={(value) => changeValue('durationSeconds', value ?? 3600)} disabled={disabled} style={{ width: '100%' }} /></Form.Item>{typeof selectedBlock.external_id === 'object' && selectedBlock.external_id !== null && !replaceExternal ? <Space direction="vertical">{secretMarker(selectedBlock.external_id)}<Button onClick={() => showReplacement(true)} disabled={disabled}>{t('components.aliyunAccess.replaceExternalId')}</Button></Space> : <Form.Item label={t('components.aliyunAccess.externalIdLabel')}><Input.Password id="aliyun-external-id" aria-label={t('components.aliyunAccess.externalIdLabel')} value={values.externalId} onChange={(event) => changeValue('externalId', event.target.value)} autoComplete="new-password" disabled={disabled} /></Form.Item>}</Space> }]} />
      </>
    )
  }

  return (
    <>
      <Space size={8} className="aliyun-access-form__advanced">
        {selectedBlock.role_name === null && (
          <Tag color="blue">{t('components.aliyunAccess.ecsRoleDiscovered')}</Tag>
        )}
        <Typography.Text type="secondary">
          {t('components.aliyunAccess.ecsDeploymentRequirement')}
        </Typography.Text>
      </Space>
      <Form.Item label={t('components.aliyunAccess.ecsRoleNameLabel')}>
        <Input
          aria-label={t('components.aliyunAccess.ecsRoleNameLabel')}
          value={values.ecsRoleName}
          onChange={(event) => changeValue('ecsRoleName', event.target.value)}
          disabled={disabled}
        />
      </Form.Item>
    </>
  )
}
