import { useCallback, useEffect, useState } from 'react'

import type { AliyunAccessMutation, AliyunCredentialMode, AliyunCredentialTransition, ConfigModule, SecretClearAction } from '../../api/configuration'

export interface AliyunFormValues {
  accessKeyId: string
  accessKeySecret: string
  sourceAccessKeyId: string
  sourceAccessKeySecret: string
  roleArn: string
  roleSessionName: string
  durationSeconds: number
  externalId: string
  ecsRoleName: string
}

type Snapshot = Record<string, unknown>

function record(value: unknown): Snapshot {
  return typeof value === 'object' && value !== null ? value as Snapshot : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

export function formValues(config: Snapshot): AliyunFormValues {
  const direct = record(config.direct_ak); const assume = record(config.assume_role); const ecs = record(config.ecs_ram_role)
  return { accessKeyId: text(direct.access_key_id), accessKeySecret: '', sourceAccessKeyId: text(assume.source_access_key_id), sourceAccessKeySecret: '', roleArn: text(assume.role_arn), roleSessionName: text(assume.role_session_name), durationSeconds: typeof assume.duration_seconds === 'number' ? assume.duration_seconds : 3600, externalId: '', ecsRoleName: text(ecs.role_name) }
}

interface BuildMutationOptions {
  mode: AliyunCredentialMode
  currentMode: AliyunCredentialMode
  selectedAction: 'replace' | 'reuse_retained' | undefined
  replaceActive: boolean
  replaceExternal: boolean
  usesCurrentDirectAsSource: boolean
  hasStoredAssumeSource: boolean
  hasStoredExternalId: boolean
}

interface BuildTransitionOptions {
  mode: AliyunCredentialMode
  currentMode: AliyunCredentialMode
  previousAction: 'clear' | 'retain'
  selectedAction: 'replace' | 'reuse_retained' | undefined
  reuseDirectSource: boolean
  reuseConfirmed: boolean
  deleteRetainedModes?: AliyunCredentialMode[]
}

const clearSecret: SecretClearAction = { $secret_action: 'clear' }

export function useAliyunAccessFormState(
  module: ConfigModule,
  config: Snapshot,
  onValuesChange?: () => void,
) {
  const [values, setValues] = useState(() => formValues(config))
  const [dirtyFields, setDirtyFields] = useState<Set<keyof AliyunFormValues>>(() => new Set())
  const resetModeState = useCallback(() => { setValues(formValues(config)); setDirtyFields(new Set()) }, [config])
  useEffect(() => { resetModeState() }, [module.name, module.revision, module.draft, module.effective, resetModeState])
  const changeValue = <K extends keyof AliyunFormValues>(name: K, value: AliyunFormValues[K]) => {
    setValues((current) => ({ ...current, [name]: value }))
    setDirtyFields((current) => new Set(current).add(name))
    onValuesChange?.()
  }
  const buildActiveMutation = (options: BuildMutationOptions): Omit<AliyunAccessMutation, 'transition'> => {
    const { mode, currentMode, selectedAction, replaceActive, replaceExternal, usesCurrentDirectAsSource, hasStoredAssumeSource, hasStoredExternalId } = options
    if (selectedAction === 'reuse_retained') return { credential_mode: mode }
    if (mode === 'direct_ak') {
      return { credential_mode: mode, direct_ak: { access_key_id: values.accessKeyId.trim(), access_key_secret: values.accessKeySecret } }
    }
    if (mode === 'assume_role') {
      const assumeRole: NonNullable<AliyunAccessMutation['assume_role']> = { role_arn: values.roleArn.trim() }
      if (!usesCurrentDirectAsSource && (replaceActive || !hasStoredAssumeSource)) {
        assumeRole.source_access_key_id = values.sourceAccessKeyId.trim()
        assumeRole.source_access_key_secret = values.sourceAccessKeySecret
      }
      if (values.roleSessionName.trim()) assumeRole.role_session_name = values.roleSessionName.trim()
      if (dirtyFields.has('durationSeconds')) assumeRole.duration_seconds = values.durationSeconds
      if (dirtyFields.has('externalId') && values.externalId) assumeRole.external_id = values.externalId
      if (dirtyFields.has('externalId') && !values.externalId && mode === currentMode && hasStoredExternalId && replaceExternal) {
        assumeRole.external_id = clearSecret
      }
      return { credential_mode: mode, assume_role: assumeRole }
    }
    return {
      credential_mode: mode,
      ecs_ram_role: {
        ...(dirtyFields.has('ecsRoleName') ? { role_name: values.ecsRoleName.trim() || null } : values.ecsRoleName.trim() ? { role_name: values.ecsRoleName.trim() } : {}),
        metadata_policy: 'v2_only',
      },
    }
  }
  const buildTransition = (options: BuildTransitionOptions): AliyunCredentialTransition => ({
    previous_mode_action: options.mode === options.currentMode ? 'clear' : options.previousAction,
    selected_mode_action: options.selectedAction ?? 'replace',
    reuse_direct_ak_as_assume_source: options.reuseDirectSource && options.reuseConfirmed,
    delete_retained_modes: options.deleteRetainedModes ?? [],
  })
  return { values, dirtyFields, changeValue, resetModeState, buildActiveMutation, buildTransition }
}
