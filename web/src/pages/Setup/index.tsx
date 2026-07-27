import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Input,
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
import { useNavigate } from 'react-router-dom'

import api, { getAPIErrorMessage } from '../../api/client'
import {
  executeConfig,
  type ConfigModule,
  type ConfigResponse,
} from '../../api/configuration'
import ConfigModuleForm from '../../components/ConfigModuleForm'
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

interface CheckedCandidate {
  moduleName: string
  revision: number
  config: Record<string, unknown>
  activationConfig?: Record<string, unknown>
}

interface SetupProps {
  mode?: 'bootstrap' | 'admin'
}

export default function Setup({ mode = 'bootstrap' }: SetupProps) {
  const navigate = useNavigate()
  const [bootstrapToken, setBootstrapToken] = useState('')
  const [verifiedToken, setVerifiedToken] = useState<string>()
  const [modules, setModules] = useState<ConfigModule[]>()
  const [selectedName, setSelectedName] = useState('core_admin')
  const [busy, setBusy] = useState(false)
  const [plan, setPlan] = useState<ConfigResponse['plan']>()
  const [checkedCandidate, setCheckedCandidate] =
    useState<CheckedCandidate>()
  const [completed, setCompleted] = useState(false)

  const selected = useMemo(
    () => modules?.find((module) => module.name === selectedName),
    [modules, selectedName],
  )

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
          getAPIErrorMessage(error, 'Could not load configuration.'),
        )
      })
      .finally(() => setBusy(false))
  }, [loadModules, mode])

  async function verifyOwnership() {
    setBusy(true)
    try {
      await loadModules(bootstrapToken)
      setVerifiedToken(bootstrapToken)
      setBootstrapToken('')
    } catch (error) {
      message.error(getAPIErrorMessage(error, 'The bootstrap token is invalid or expired.'))
    } finally {
      setBusy(false)
    }
  }

  function invalidatePlan() {
    setPlan(undefined)
    setCheckedCandidate(undefined)
  }

  async function runDryRun(values: Record<string, unknown>) {
    if (!selected) return
    setBusy(true)
    invalidatePlan()
    const password = typeof values.password === 'string' ? values.password : undefined
    const config = cleanCandidate(values, selected)
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
      setPlan(planned.plan)
      if (!planned.plan?.valid) {
        message.error(
          planned.plan?.message ?? 'Configuration validation failed.',
        )
        return
      }
      setCheckedCandidate({
        moduleName: selected.name,
        revision: selected.revision,
        config,
        activationConfig,
      })
    } catch (error) {
      message.error(
        getAPIErrorMessage(
          error,
          error instanceof Error
            ? error.message
            : 'Could not check configuration.',
        ),
      )
    } finally {
      setBusy(false)
    }
  }

  async function activateCheckedCandidate() {
    if (
      !checkedCandidate
      || !selected
      || checkedCandidate.moduleName !== selected.name
      || checkedCandidate.revision !== selected.revision
    ) {
      invalidatePlan()
      return
    }
    setBusy(true)
    let mutationStarted = false
    try {
      mutationStarted = true
      const saved = await executeConfig(
        {
          action: 'save_draft',
          module: checkedCandidate.moduleName,
          expected_revision: checkedCandidate.revision,
          config: checkedCandidate.config,
        },
        verifiedToken,
      )
      const validated = await executeConfig(
        {
          action: 'validate',
          module: checkedCandidate.moduleName,
          expected_revision: saved.module!.revision,
        },
        verifiedToken,
      )
      const activated = await executeConfig(
        {
          action: 'activate',
          module: checkedCandidate.moduleName,
          expected_revision: validated.module!.revision,
          validation_id: validated.validation!.validation_id,
          idempotency_key: crypto.randomUUID(),
          config: checkedCandidate.activationConfig,
          confirm_impact:
            checkedCandidate.moduleName === 'agentic_db_purchase',
        },
        verifiedToken,
      )
      if (checkedCandidate.moduleName === 'core_admin') {
        await api.post(
          '/auth/login',
          {
            username: String(
              checkedCandidate.config.username ?? 'admin',
            ),
            password: checkedCandidate.activationConfig?.password,
          },
          { pasSkipAuthRedirect: true },
        )
        setVerifiedToken(undefined)
      }
      message.success(`${checkedCandidate.moduleName} is active`)
      invalidatePlan()
      await loadModules(
        checkedCandidate.moduleName === 'core_admin'
          ? undefined
          : verifiedToken,
      )
      if (
        activated.system_state === 'READY'
        && checkedCandidate.moduleName === 'core_admin'
      ) {
        setCompleted(true)
      }
    } catch (error) {
      invalidatePlan()
      message.error(
        getAPIErrorMessage(
          error,
          error instanceof Error
            ? error.message
            : 'Could not activate configuration.',
        ),
      )
      if (mutationStarted) {
        try {
          await loadModules(verifiedToken)
        } catch (refreshError) {
          message.error(
            getAPIErrorMessage(
              refreshError,
              'Could not refresh configuration state.',
            ),
          )
        }
      }
    } finally {
      setBusy(false)
    }
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
      message.success(`${selected.name} skipped for now`)
    } catch (error) {
      message.error(getAPIErrorMessage(error, 'Could not skip this module.'))
      invalidatePlan()
      try {
        await loadModules(verifiedToken)
      } catch (refreshError) {
        message.error(
          getAPIErrorMessage(
            refreshError,
            'Could not refresh configuration state.',
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
        <section className="setup-ownership-panel" aria-labelledby="setup-title">
          <div className="setup-mark" aria-hidden="true">
            <SafetyCertificateOutlined />
          </div>
          <Title id="setup-title" level={1}>
            Claim this installation
          </Title>
          <Paragraph>
            Enter the one-time bootstrap token from the server terminal. It remains only in this tab
            and is never saved by the browser.
          </Paragraph>
          <label className="setup-token-label" htmlFor="bootstrap-token">
            Bootstrap token
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
            Verify and continue
          </Button>
          <Text type="secondary">
            Lost the token? Run the Pod-local <code>pas config bootstrap-token issue</code> command.
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

  return (
    <main className="setup-shell">
      <header className="setup-header">
        <div>
          <Title level={2}>Configure the server</Title>
          <Paragraph>Activate only the capabilities this installation needs. Optional modules can be skipped and configured later.</Paragraph>
        </div>
        {mode === 'bootstrap' && completed && (
          <Button type="primary" onClick={() => navigate('/dashboard')}>
            Enter administration console <RightOutlined />
          </Button>
        )}
      </header>

      <div className="setup-workspace">
        <nav className="setup-modules" aria-label="Configuration modules">
          {modules.map((module) => (
            <button
              key={module.name}
              type="button"
              className={`setup-module-row ${module.name === selectedName ? 'is-selected' : ''}`}
              onClick={() => {
                setSelectedName(module.name)
                invalidatePlan()
              }}
              disabled={busy}
            >
              <span>
                <strong>{module.name.replace(/_/g, ' ')}</strong>
                <small>
                  {module.dependencies.length > 0
                    ? `Requires ${module.dependencies.join(', ')}`
                    : module.name === 'core_admin'
                      ? 'Required recovery access'
                      : 'Independent module'}
                </small>
              </span>
              <Tag color={stateColor(module.workflow_state)}>{module.workflow_state}</Tag>
            </button>
          ))}
        </nav>

        <section className="setup-editor" aria-labelledby="module-title">
          <div className="setup-editor-heading">
            <div>
              <Title id="module-title" level={3}>
                {selected.name.replace(/_/g, ' ')}
              </Title>
              <Text type="secondary">Revision {selected.revision}</Text>
            </div>
            {selected.workflow_state === 'ACTIVE' && <CheckCircleOutlined className="setup-active-icon" />}
          </div>

          {selected.name === 'agentic_db_purchase' && (
            <Alert
              type="warning"
              showIcon
              message="Activation permits future billable database purchases"
              description="Review the purchase policy and Aliyun dependency before continuing."
            />
          )}
          {plan && (
            <Alert
              type={plan.valid ? 'success' : 'error'}
              showIcon
              message={plan.valid ? 'Dry-run checks passed' : 'Dry-run checks failed'}
              description={plan.message}
            />
          )}
          {plan?.external_validation
            && plan.external_validation.checks.length > 0 && (
            <Alert
              type="info"
              showIcon
              message="OpenAPI connectivity checked by the backend Pod"
              description={
                <Space direction="vertical" size={2}>
                  {plan.external_validation.checks.map((check) => (
                    <Text
                      key={`${check.service}:${check.endpoint}`}
                      code
                    >
                      {check.service}: {check.endpoint} ({check.status})
                    </Text>
                  ))}
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
              message="Last validation failed"
              description={selected.last_error_code}
            />
          )}
          {coreAdminLocked && (
            <Alert
              type="info"
              showIcon
              message="Administrator identity is managed separately"
              description="Use account security to change the administrator password. Bootstrap credentials cannot be reused after initial setup."
            />
          )}

          <ConfigModuleForm
            key={`${selected.name}:${selected.revision}`}
            module={selected}
            disabled={busy || coreAdminLocked}
            onSubmit={runDryRun}
            onValuesChange={invalidatePlan}
            formId="selected-module-form"
          />

          <div className="setup-actions">
            {!coreAdminLocked && !checkedCandidate && (
              <Button type="primary" htmlType="submit" form="selected-module-form" loading={busy}>
                Run dry-run
              </Button>
            )}
            {!coreAdminLocked && checkedCandidate && (
              <>
                <Button
                  type="primary"
                  loading={busy}
                  onClick={activateCheckedCandidate}
                >
                  Activate module
                </Button>
                <Button
                  htmlType="submit"
                  form="selected-module-form"
                  disabled={busy}
                >
                  Run dry-run again
                </Button>
              </>
            )}
            {selected.name !== 'core_admin' && selected.workflow_state !== 'ACTIVE' && (
              <Button onClick={skipModule} disabled={busy}>
                Skip for now
              </Button>
            )}
            <Space className="setup-state-note">
              <Text type="secondary">Changes become effective only after validation and activation.</Text>
            </Space>
          </div>
        </section>
      </div>
    </main>
  )
}
