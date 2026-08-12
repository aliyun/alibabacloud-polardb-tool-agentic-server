import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Skeleton,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  LockOutlined,
  RightOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import api, { getAPIErrorMessage } from '../../api/client'
import {
  executeConfig,
  type AliyunAccessMutation,
  type ConfigModule,
  type ConfigResponse,
} from '../../api/configuration'
import AliyunAccessForm from '../../components/AliyunAccessForm'
import ConfigModuleForm from '../../components/ConfigModuleForm'
import LanguageSwitcher from '../../components/LanguageSwitcher'
import { formatDateTime } from '../../i18n/format'
import {
  isConfirmableExternalFailure,
  normalizeDryRunDetails,
  type SafeDryRunDetails,
} from './workflowSafety'
import {
  type ActivationCandidate,
  useActivationWorkflow,
} from './useActivationWorkflow'
import './Setup.css'

const { Title, Paragraph, Text } = Typography

function stateColor(state: string) {
  if (state === 'ACTIVE') return 'success'
  if (state === 'ERROR') return 'error'
  if (state === 'VALIDATED') return 'processing'
  return 'default'
}

function cleanCandidate(
  values: Record<string, unknown>,
  module: ConfigModule,
) {
  const secretFields = new Set(module.ui_hints?.secret_fields ?? [])
  return Object.fromEntries(
    Object.entries(values).filter(([name, value]) => {
      if (name === 'password') return false
      if (secretFields.has(name) && (value === '' || value == null)) return false
      return value !== undefined
    }),
  )
}

interface SetupProps {
  mode?: 'bootstrap' | 'admin'
  onEnterConsole?: () => void
}

export default function Setup({
  mode = 'bootstrap',
  onEnterConsole,
}: SetupProps) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [bootstrapToken, setBootstrapToken] = useState('')
  const [verifiedToken, setVerifiedToken] = useState<string>()
  const [modules, setModules] = useState<ConfigModule[]>()
  const [selectedName, setSelectedName] = useState('core_admin')
  const [busy, setBusy] = useState(false)
  const [plan, setPlan] = useState<ConfigResponse['plan']>()
  const [planDetails, setPlanDetails] = useState<SafeDryRunDetails>()
  const [checkedCandidate, setCheckedCandidate] =
    useState<ActivationCandidate>()
  const [completed, setCompleted] = useState(false)
  const [confirmationOpen, setConfirmationOpen] = useState(false)
  const [focusTarget, setFocusTarget] = useState<'dryRun' | 'heading'>()
  const dryRunButtonRef = useRef<HTMLButtonElement>(null)
  const moduleHeadingRef = useRef<HTMLHeadingElement>(null)

  const selected = useMemo(
    () => modules?.find((module) => module.name === selectedName),
    [modules, selectedName],
  )

  const moduleDisplayName = useCallback((name: string) => (
    t(`setup.moduleNames.${name}`, {
      defaultValue: name.replace(/_/g, ' '),
    })
  ), [t])

  const loadModules = useCallback(async (token?: string) => {
    const response = await executeConfig({ action: 'describe' }, token)
    setModules(response.modules ?? [])
    setCompleted(response.system_state === 'READY')
    return response
  }, [])

  useEffect(() => {
    if (mode !== 'admin') return
    setBusy(true)
    loadModules()
      .catch((error) => {
        message.error(
          getAPIErrorMessage(error, t('setup.loadFailed')),
        )
      })
      .finally(() => setBusy(false))
  }, [loadModules, mode, t])

  useEffect(() => {
    if (mode !== 'admin' || !modules) return
    const requestedName = searchParams.get('module')
    if (
      requestedName
      && requestedName !== selectedName
      && modules.some((module) => module.name === requestedName)
    ) {
      setSelectedName(requestedName)
      invalidatePlan()
    }
  }, [mode, modules, searchParams, selectedName])

  useEffect(() => {
    if (mode !== 'admin' || !selected) return
    const requestedField = searchParams.get('field')
    if (
      !requestedField
      || !selected.schema.properties?.[requestedField]
    ) return
    const timer = window.setTimeout(() => {
      const target = document.getElementById(requestedField)
      target?.scrollIntoView?.({ block: 'center' })
      target?.focus()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [mode, searchParams, selected])

  function selectModule(name: string) {
    setSelectedName(name)
    invalidatePlan()
    if (mode === 'admin') {
      const nextSearchParams = new URLSearchParams(searchParams)
      nextSearchParams.set('module', name)
      nextSearchParams.delete('field')
      setSearchParams(nextSearchParams)
    }
  }

  async function verifyOwnership() {
    setBusy(true)
    try {
      await loadModules(bootstrapToken)
      setVerifiedToken(bootstrapToken)
      setBootstrapToken('')
    } catch (error) {
      message.error(getAPIErrorMessage(error, t('setup.tokenInvalid')))
    } finally {
      setBusy(false)
    }
  }

  function invalidatePlan() {
    setPlan(undefined)
    setPlanDetails(undefined)
    setCheckedCandidate(undefined)
    setConfirmationOpen(false)
  }

  useEffect(() => {
    if (!focusTarget || busy || confirmationOpen) return
    const target = focusTarget === 'dryRun'
      ? dryRunButtonRef.current
      : moduleHeadingRef.current
    target?.focus()
    setFocusTarget(undefined)
  }, [busy, confirmationOpen, focusTarget, modules, selectedName])

  const activateCheckedCandidate = useActivationWorkflow({
    candidate: checkedCandidate,
    selected,
    bootstrapToken: verifiedToken,
    execute: executeConfig,
    loadModules,
    loginCoreAdmin: async (candidate) => {
      await api.post(
        '/auth/login',
        {
          username: String(candidate.config.username ?? 'admin'),
          password: candidate.activationConfig?.password,
        },
        { pasSkipAuthRedirect: true },
      )
      setVerifiedToken(undefined)
    },
    invalidate: invalidatePlan,
    setBusy,
    onFailureAfterRefresh: () => setFocusTarget('dryRun'),
    onSuccessAfterRefresh: () => setFocusTarget('heading'),
    setCompleted,
    showError: (error, action) => {
      message.error(getAPIErrorMessage(
        error,
        action === 'refresh' ? t('setup.refreshFailed') : t('setup.activateFailed'),
      ))
    },
    showProofError: () => message.error(t('setup.invalidActivationProof')),
    showSuccess: (module) => message.success(t('setup.activeSuccess', {
      module: moduleDisplayName(module),
    })),
  })

  async function runDryRun(
    values: Record<string, unknown> | AliyunAccessMutation,
  ) {
    if (!selected) return
    setBusy(true)
    invalidatePlan()
    const candidateValues = values as Record<string, unknown>
    const password = typeof candidateValues.password === 'string'
      ? candidateValues.password
      : undefined
    const config = cleanCandidate(candidateValues, selected)
    const activationConfig =
      selected.name === 'core_admin' ? { password } : undefined
    try {
      const planned = await executeConfig(
        {
          action: 'plan',
          module: selected.name,
          config: { ...config, ...activationConfig },
        },
        verifiedToken,
      )
      const nextPlan = planned.plan
      setPlan(nextPlan)
      setPlanDetails(normalizeDryRunDetails(
        nextPlan,
        candidateValues.credential_mode,
      ))
      if (!nextPlan?.valid) {
        if (nextPlan && isConfirmableExternalFailure(selected.name, nextPlan)) {
          setCheckedCandidate({
            moduleName: selected.name,
            revision: selected.revision,
            config,
            activationConfig,
            confirmExternalFailure: true,
          })
        } else {
          message.error(t('setup.validationFailed'))
        }
        return
      }
      setCheckedCandidate({
        moduleName: selected.name,
        revision: selected.revision,
        config,
        activationConfig,
        confirmExternalFailure: false,
      })
    } catch (error) {
      message.error(
        getAPIErrorMessage(
          error,
          error instanceof Error
            ? error.message
            : t('setup.checkFailed'),
        ),
      )
    } finally {
      setBusy(false)
    }
  }

  function requestActivation() {
    if (!checkedCandidate) return
    if (checkedCandidate.confirmExternalFailure) {
      setConfirmationOpen(true)
      return
    }
    void activateCheckedCandidate()
  }

  function cancelExternalFailureConfirmation() {
    if (busy) return
    setFocusTarget('dryRun')
    invalidatePlan()
  }

  async function skipModule() {
    if (!selected) return
    setBusy(true)
    try {
      await executeConfig(
        {
          action: 'skip',
          module: selected.name,
          expected_revision: selected.revision,
        },
        verifiedToken,
      )
      invalidatePlan()
      await loadModules(verifiedToken)
      message.success(t('setup.skippedSuccess', {
        module: moduleDisplayName(selected.name),
      }))
    } catch (error) {
      message.error(getAPIErrorMessage(error, t('setup.skipFailed')))
      invalidatePlan()
      try {
        await loadModules(verifiedToken)
      } catch (refreshError) {
        message.error(
          getAPIErrorMessage(
            refreshError,
            t('setup.refreshFailed'),
          ),
        )
      }
    } finally {
      setBusy(false)
    }
  }

  if (mode === 'bootstrap' && !verifiedToken && !modules) {
    return (
      <main className="setup-shell setup-ownership">
        <div className="setup-language"><LanguageSwitcher /></div>
        <section className="setup-ownership-panel" aria-labelledby="setup-title">
          <div className="setup-mark" aria-hidden="true">
            <SafetyCertificateOutlined />
          </div>
          <Title id="setup-title" level={1}>
            {t('setup.claimTitle')}
          </Title>
          <Paragraph>
            {t('setup.claimDescription')}
          </Paragraph>
          <label className="setup-token-label" htmlFor="bootstrap-token">
            {t('setup.bootstrapToken')}
          </label>
          <Input.Password
            id="bootstrap-token"
            value={bootstrapToken}
            onChange={(event) => setBootstrapToken(event.target.value)}
            prefix={<LockOutlined />}
            autoComplete="off"
            autoFocus
            onPressEnter={verifyOwnership}
          />
          <Button
            type="primary"
            size="large"
            block
            loading={busy}
            disabled={!bootstrapToken}
            onClick={verifyOwnership}
          >
            {t('setup.verifyContinue')}
          </Button>
          <Text type="secondary">
            {t('setup.lostTokenPrefix')} <code>pas config bootstrap-token issue</code> {t('setup.commandSuffix')}
          </Text>
        </section>
      </main>
    )
  }

  if (!modules || !selected) {
    return (
      <main className="setup-shell">
        <Skeleton active paragraph={{ rows: 8 }} />
      </main>
    )
  }

  const coreAdminLocked =
    mode === 'admin'
    && selected.name === 'core_admin'
    && selected.workflow_state === 'ACTIVE'
  const builtInModuleLocked = selected.configurable === false
  const moduleLocked = coreAdminLocked || builtInModuleLocked
  const dryRunDetails = planDetails

  return (
    <main className="setup-shell">
      {mode === 'bootstrap' && <div className="setup-language"><LanguageSwitcher /></div>}
      <header className="setup-header">
        <div>
          <Title level={2}>{t('setup.configureTitle')}</Title>
          <Paragraph>{t('setup.configureDescription')}</Paragraph>
        </div>
        {mode === 'bootstrap' && completed && (
          <Button
            type="primary"
            onClick={() => {
              if (onEnterConsole) {
                onEnterConsole()
              } else {
                navigate('/dashboard')
              }
            }}
          >
            {t('setup.enterConsole')} <RightOutlined />
          </Button>
        )}
      </header>

      <div className="setup-workspace">
        <nav className="setup-modules" aria-label={t('setup.modulesLabel')}>
          {modules.map((module) => (
            <button
              key={module.name}
              type="button"
              className={`setup-module-row ${module.name === selectedName ? 'is-selected' : ''}`}
              onClick={() => selectModule(module.name)}
              disabled={busy}
            >
              <span>
                <strong>{moduleDisplayName(module.name)}</strong>
                <small>
                  {module.dependencies.length > 0
                    ? t('setup.requires', {
                      dependencies: module.dependencies
                        .map(moduleDisplayName)
                        .join(', '),
                    })
                    : module.name === 'core_admin'
                      ? t('setup.requiredRecovery')
                      : t(`setup.moduleDescriptions.${module.name}`, {
                        defaultValue: t('setup.independentModule'),
                      })}
                </small>
              </span>
              <Tag color={stateColor(module.workflow_state)}>{module.workflow_state}</Tag>
            </button>
          ))}
        </nav>

        <section className="setup-editor" aria-labelledby="module-title">
          <div className="setup-editor-heading">
            <div>
              <Title id="module-title" level={3} ref={moduleHeadingRef} tabIndex={-1}>
                {moduleDisplayName(selected.name)}
              </Title>
              <Text type="secondary">{t('setup.revision', { revision: selected.revision })}</Text>
            </div>
            {selected.workflow_state === 'ACTIVE' && <CheckCircleOutlined className="setup-active-icon" />}
          </div>

          {plan && dryRunDetails && (
            <Alert
              type={dryRunDetails.valid ? 'success' : 'error'}
              showIcon
              message={dryRunDetails.valid ? t('setup.dryRunPassed') : t('setup.dryRunFailed')}
              description={
                <Space direction="vertical" size={8} className="setup-dry-run-details">
                  {dryRunDetails.guidance && (
                    <Text>{t(`setup.dryRunGuidance.${dryRunDetails.guidance}`)}</Text>
                  )}
                  {dryRunDetails.checks.some((check) => check.service === 'polardb') && (
                    <Alert
                      type="warning"
                      showIcon
                      message={t('setup.dryRunPermissionScopeTitle')}
                      description={t('setup.dryRunPermissionScopeDescription')}
                    />
                  )}
                  <Descriptions size="small" column={1} bordered>
                    {dryRunDetails.credentialMode && (
                      <Descriptions.Item label={t('setup.dryRunMode')}>
                        {t(`components.aliyunAccess.modes.${dryRunDetails.credentialMode}.title`)}
                      </Descriptions.Item>
                    )}
                    <Descriptions.Item label={t('setup.dryRunStatus')}>
                      {dryRunDetails.valid ? t('setup.dryRunStatusPassed') : t('setup.dryRunStatusFailed')}
                    </Descriptions.Item>
                    {dryRunDetails.errorCode && (
                      <Descriptions.Item label={t('setup.dryRunErrorCode')}>
                        <Text code>{dryRunDetails.errorCode}</Text>
                      </Descriptions.Item>
                    )}
                    {dryRunDetails.requestId && (
                      <Descriptions.Item label={t('setup.dryRunRequestIdLabel')}>
                        <Text code>{dryRunDetails.requestId}</Text>
                      </Descriptions.Item>
                    )}
                    {dryRunDetails.checks.map((check) => (
                      <Descriptions.Item
                        key={`${check.service}:${check.endpoint}`}
                        label={t('setup.dryRunService', { service: check.service })}
                      >
                        <Space direction="vertical" size={2}>
                          <Text>{t('setup.dryRunEndpoint', { endpoint: check.endpoint })}</Text>
                          <Text>{t('setup.dryRunCheckStatus', { status: check.status })}</Text>
                          {check.identityHint && (
                            <Text>{t('setup.dryRunIdentity', { identity: check.identityHint })}</Text>
                          )}
                          {check.expiresAt !== undefined && (
                            <Text>
                              {t('setup.dryRunExpiration', {
                                expiration: formatDateTime(
                                  check.expiresAt * 1000,
                                  i18n.language,
                                ),
                              })}
                            </Text>
                          )}
                          {check.requestId && (
                            <Text>{t('setup.dryRunRequestId', { requestId: check.requestId })}</Text>
                          )}
                        </Space>
                      </Descriptions.Item>
                    ))}
                  </Descriptions>
                </Space>
              }
            />
          )}
          {!plan
            && selected.workflow_state === 'ERROR'
            && selected.last_error_code && (
            <Alert
              type="error"
              showIcon
              message={t('setup.lastValidationFailed')}
              description={selected.last_error_code}
            />
          )}
          {coreAdminLocked && (
            <Alert
              type="info"
              showIcon
              message={t('setup.adminManagedSeparately')}
              description={t('setup.adminManagedDescription')}
            />
          )}
          {builtInModuleLocked && (
            <Alert
              type="info"
              showIcon
              message={t('setup.builtInModuleActive')}
              description={t('setup.builtInModuleActiveDescription')}
            />
          )}

          {!builtInModuleLocked && (selected.name === 'aliyun_access' ? (
            <AliyunAccessForm
              key={`${selected.name}:${selected.revision}`}
              module={selected}
              disabled={busy || moduleLocked}
              onSubmit={runDryRun}
              onValuesChange={invalidatePlan}
              formId="selected-module-form"
            />
          ) : (
            <ConfigModuleForm
              key={`${selected.name}:${selected.revision}`}
              module={selected}
              moduleLabel={moduleDisplayName(selected.name)}
              disabled={busy || moduleLocked}
              onSubmit={runDryRun}
              onValuesChange={invalidatePlan}
              formId="selected-module-form"
            />
          ))}

          <div className="setup-actions">
            {!moduleLocked && !checkedCandidate && (
              <Button ref={dryRunButtonRef} type="primary" htmlType="submit" form="selected-module-form" loading={busy}>
                {t('setup.runDryRun')}
              </Button>
            )}
            {!moduleLocked && checkedCandidate && (
              <>
                <Button
                  type="primary"
                  loading={busy}
                  onClick={requestActivation}
                >
                  {checkedCandidate.confirmExternalFailure
                    ? t('setup.saveEnableAnyway')
                    : t('setup.activateModule')}
                </Button>
                <Button
                  htmlType="submit"
                  form="selected-module-form"
                  disabled={busy}
                >
                  {t('setup.runDryRunAgain')}
                </Button>
              </>
            )}
            {!moduleLocked && selected.name !== 'core_admin' && selected.workflow_state !== 'ACTIVE' && (
              <Button onClick={skipModule} disabled={busy}>
                {t('setup.skipForNow')}
              </Button>
            )}
            <Space className="setup-state-note">
              <Text type="secondary">{t('setup.activationNote')}</Text>
            </Space>
          </div>
        </section>
      </div>
      <Modal
        open={confirmationOpen}
        title={t('setup.externalFailureTitle')}
        okText={t('setup.confirmActivation')}
        cancelText={t('common.cancel')}
        confirmLoading={busy}
        closable={!busy}
        keyboard={!busy}
        maskClosable={!busy}
        focusTriggerAfterClose={false}
        modalRender={(modal) => (
          <div
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !busy) {
                event.preventDefault()
                event.stopPropagation()
                cancelExternalFailureConfirmation()
              }
            }}
          >
            {modal}
          </div>
        )}
        onCancel={cancelExternalFailureConfirmation}
        onOk={() => void activateCheckedCandidate(true)}
      >
        <Space direction="vertical" size={12}>
          <Paragraph>{t('setup.externalFailureConfirmation')}</Paragraph>
          <Alert
            type="warning"
            showIcon
            message={t('setup.externalFailureRepair')}
          />
        </Space>
      </Modal>
    </main>
  )
}
