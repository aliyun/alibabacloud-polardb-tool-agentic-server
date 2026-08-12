import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  List,
  Row,
  Select,
  Skeleton,
  Space,
  Steps,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExportOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'

import { getAPIErrorMessage } from '../../api/client'
import {
  createDedicatedPool,
  getDedicatedPurchaseProfile,
  getDedicatedReadiness,
  type CreateDedicatedPoolInput,
  type DedicatedPurchaseProfile,
  type DedicatedReadiness,
} from '../../api/dedicatedPools'
import {
  listPermissionTemplates,
  type PermissionTemplate,
} from '../../api/permissionTemplates'
import PageContainer from '../../components/PageContainer'
import './DedicatedPoolCreatePage.css'

const { Paragraph, Text, Title } = Typography

interface PoolFormValues {
  name: string
  target_size: number
  max_total_members: number
  max_member_purchases_per_hour: number
  max_create_requests_per_agent_per_hour: number
  max_delete_requests_per_agent_per_hour: number
  region_id: string
  zone_id: string
  vpc_id: string
  vswitch_id: string
  security_ip_list?: string
  storage_type: string
  permission_template_revision_id: string
  reclaim_policy: 'destroy' | 'sanitize_and_reuse'
  delete_cooldown_duration_hours?: number
  available_health_check_interval_seconds: number
  available_health_stale_after_seconds: number
}

const STEP_FIELDS: (keyof PoolFormValues)[][] = [
  [],
  ['name', 'target_size', 'max_total_members'],
  ['region_id', 'zone_id', 'vpc_id', 'vswitch_id', 'storage_type'],
  [
    'permission_template_revision_id',
    'reclaim_policy',
    'available_health_check_interval_seconds',
    'available_health_stale_after_seconds',
  ],
]

function validRegion(value?: string) {
  return Boolean(value && /^[a-z0-9-]+$/.test(value))
}

export default function DedicatedPoolCreatePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [form] = Form.useForm<PoolFormValues>()
  const [step, setStep] = useState(0)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [readiness, setReadiness] = useState<DedicatedReadiness | null>(null)
  const [profile, setProfile] = useState<DedicatedPurchaseProfile | null>(null)
  const [templates, setTemplates] = useState<PermissionTemplate[]>([])
  const regionId = Form.useWatch('region_id', form)
  const permissionRevisionId = Form.useWatch(
    'permission_template_revision_id',
    form,
  )

  useEffect(() => {
    const load = async () => {
      setLoading(true)
      setError(null)
      try {
        const [readinessResponse, profileResponse, templatesResponse] =
          await Promise.all([
            getDedicatedReadiness(),
            getDedicatedPurchaseProfile(),
            listPermissionTemplates(),
          ])
        setReadiness(readinessResponse.data)
        setProfile(profileResponse.data)
        setTemplates(templatesResponse.data)
        form.setFieldsValue({
          storage_type: profileResponse.data.default_storage_type,
          permission_template_revision_id:
            readinessResponse.data.permission_template.default_revision_id ??
            templatesResponse.data[0]?.revisions[0]?.id,
        })
      } catch (requestError) {
        setError(
          getAPIErrorMessage(requestError, t('pool.createWizardLoadFailed')),
        )
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [form, t])

  const revisionOptions = useMemo(
    () =>
      templates.flatMap((template) =>
        template.revisions.map((revision) => ({
          value: revision.id,
          label:
            template.id === 'builtin-mysql-default'
              ? `${t('pool.agentDefaultPermissions')} · v${revision.revision}`
              : `${template.name} · v${revision.revision}`,
        })),
      ),
    [templates, t],
  )

  const selectedRevision = useMemo(
    () =>
      templates
        .flatMap((template) => template.revisions)
        .find((revision) => revision.id === permissionRevisionId),
    [permissionRevisionId, templates],
  )

  const blockerCodes = readiness?.blocking_reasons ?? []
  const canPrewarm = blockerCodes.length === 0
  const workerBlocked = blockerCodes.some((code) => [
    'DEDICATED_WORKER_DISABLED',
    'DEDICATED_WORKER_NOT_RUNNING',
  ].includes(code))
  const aliyunAccessBlocked = blockerCodes.some((code) => [
    'ALIYUN_ACCESS_NOT_CONFIGURED',
    'ALIYUN_ACCESS_VALIDATION_FAILED',
  ].includes(code))

  const next = async () => {
    try {
      await form.validateFields(STEP_FIELDS[step])
      setStep((value) => Math.min(3, value + 1))
    } catch {
      // Ant Design renders field-addressable validation feedback.
    }
  }

  const submit = async (values: PoolFormValues) => {
    if (!profile) return
    setSubmitting(true)
    setError(null)
    const allValues = {
      ...form.getFieldsValue(true),
      ...values,
    } as PoolFormValues
    const input: CreateDedicatedPoolInput = {
      ...allValues,
      purchase_profile_id: profile.profile_id,
      purchase_profile_revision: profile.revision,
      lifecycle_admin_policy: 'pas_managed',
      delete_cooldown_duration_hours:
        allValues.delete_cooldown_duration_hours ?? null,
    }
    try {
      const response = await createDedicatedPool(input)
      message.success(
        canPrewarm
          ? t('pool.dedicatedCreatedPrewarming')
          : t('pool.dedicatedCreatedNotStarted'),
      )
      navigate(`/pool/dedicated/${encodeURIComponent(response.data.id)}`)
    } catch (requestError) {
      setError(getAPIErrorMessage(requestError, t('pool.actionFailed')))
    } finally {
      setSubmitting(false)
    }
  }

  const steps = [
    t('pool.createSteps.prerequisites'),
    t('pool.createSteps.capacity'),
    t('pool.createSteps.network'),
    t('pool.createSteps.access'),
  ]

  return (
    <PageContainer
      title={t('pool.createWizardTitle')}
      description={t('pool.createWizardDescription')}
      actions={(
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/pool')}>
          {t('pool.backToPools')}
        </Button>
      )}
    >
      <div className="dedicated-create">
        <Steps
          current={step}
          responsive
          items={steps.map((title) => ({ title }))}
          aria-label={t('pool.createProgress')}
        />

        {error && (
          <Alert role="alert" type="error" showIcon message={error} />
        )}

        {loading ? (
          <Skeleton active paragraph={{ rows: 10 }} />
        ) : (
          <Form<PoolFormValues>
            form={form}
            layout="vertical"
            requiredMark="optional"
            onFinish={(values) => void submit(values)}
            initialValues={{
              target_size: 1,
              max_total_members: 3,
              max_member_purchases_per_hour: 2,
              max_create_requests_per_agent_per_hour: 10,
              max_delete_requests_per_agent_per_hour: 10,
              reclaim_policy: 'destroy',
              available_health_check_interval_seconds: 300,
              available_health_stale_after_seconds: 600,
            }}
          >
            <section className="dedicated-create-step" aria-live="polite">
              {step === 0 && readiness && (
                <>
                  <Title level={4}>{t('pool.runtimePrerequisites')}</Title>
                  <Paragraph type="secondary">
                    {t('pool.runtimePrerequisitesDescription')}
                  </Paragraph>
                  {readiness.simulation_mode && (
                    <Alert
                      type="warning"
                      showIcon
                      message={t('pool.simulationTitle')}
                      description={t('pool.simulationDescription')}
                    />
                  )}
                  <Alert
                    type={readiness.preparation_mode === 'openapi_only'
                      ? 'warning'
                      : 'info'}
                    showIcon
                    message={readiness.preparation_mode === 'openapi_only'
                      ? t('pool.openapiOnlyTitle')
                      : t('pool.preparationModeCurrent', {
                        mode: t('pool.preparationModeValues.full'),
                      })}
                    description={(
                      <Space direction="vertical" size={4}>
                        <Text>
                          {readiness.preparation_mode === 'openapi_only'
                            ? t('pool.openapiOnlyDescription')
                            : t('pool.fullPreparationDescription')}
                        </Text>
                        <Text type="secondary">
                          {t('pool.preparationModeGlobalHint')}
                        </Text>
                      </Space>
                    )}
                    action={(
                      <Link to="/settings/configuration?module=runtime_policy&field=dedicated_pool_preparation_mode">
                        {t('pool.configurePreparationMode')}
                      </Link>
                    )}
                  />
                  <List
                    className="dedicated-readiness-list"
                    dataSource={[
                      {
                        label: t('pool.workerPrerequisite'),
                        ready: readiness.worker.active_worker_count > 0,
                        detail: (
                          <Space direction="vertical" size={2}>
                            <Text>{t('pool.workerPrerequisiteDescription')}</Text>
                            <Text type="secondary">
                              {t('pool.workerPrerequisiteDetail', {
                                count: readiness.worker.active_worker_count,
                              })}
                            </Text>
                          </Space>
                        ),
                        actionLabel: workerBlocked
                          ? t('pool.configureWorker')
                          : undefined,
                        actionTo: workerBlocked
                          ? '/settings/configuration?module=runtime_policy'
                          : undefined,
                      },
                      {
                        label: t('pool.aliyunPrerequisite'),
                        ready:
                          readiness.simulation_mode || (
                            readiness.aliyun_access.configured
                            && readiness.aliyun_access.validated
                          ),
                        detail: (
                          <Space direction="vertical" size={2}>
                            <Text>{t('pool.aliyunPrerequisiteDescription')}</Text>
                            <Text type="secondary">
                              {readiness.aliyun_access.configured
                                ? t('pool.aliyunCredentialMode', {
                                  mode: readiness.aliyun_access.credential_mode,
                                })
                                : t('pool.aliyunNotConfiguredShort')}
                            </Text>
                          </Space>
                        ),
                        actionLabel: aliyunAccessBlocked
                          ? t('pool.configureAliyunCredentials')
                          : undefined,
                        actionTo: aliyunAccessBlocked
                          ? '/settings/configuration?module=aliyun_access'
                          : undefined,
                      },
                      {
                        label: t('pool.permissionPrerequisite'),
                        ready: readiness.permission_template.valid,
                        detail: t('pool.permissionPrerequisiteDescription'),
                        actionLabel: undefined,
                        actionTo: undefined,
                      },
                    ]}
                    renderItem={(item) => (
                      <List.Item
                        actions={item.actionTo && item.actionLabel ? [
                          <Link key={item.actionTo} to={item.actionTo}>
                            {item.actionLabel}
                          </Link>,
                        ] : undefined}
                      >
                        <List.Item.Meta
                          avatar={item.ready ? (
                            <CheckCircleOutlined className="readiness-ready" />
                          ) : (
                            <CloseCircleOutlined className="readiness-blocked" />
                          )}
                          title={item.label}
                          description={item.detail}
                        />
                        <Tag color={item.ready ? 'success' : 'error'}>
                          {item.ready
                            ? t('pool.prerequisiteReady')
                            : t('pool.prerequisiteBlocked')}
                        </Tag>
                      </List.Item>
                    )}
                  />
                  {blockerCodes.length === 0 ? (
                    <Alert
                      type="success"
                      showIcon
                      message={t('pool.allPrerequisitesReady')}
                    />
                  ) : (
                    <Alert
                      type="warning"
                      showIcon
                      message={t('pool.poolWillNotStart')}
                      description={(
                        <ul className="dedicated-blocker-list">
                          {blockerCodes.map((code) => (
                            <li key={code}>
                              {t(`pool.blockingReasons.${code}`)}
                            </li>
                          ))}
                        </ul>
                      )}
                    />
                  )}
                </>
              )}

              {step === 1 && (
                <>
                  <Title level={4}>{t('pool.capacityTitle')}</Title>
                  <Alert
                    type="info"
                    showIcon
                    message={t('pool.capacityCostTitle')}
                    description={t('pool.capacityCostDescription')}
                  />
                  <Form.Item
                    name="name"
                    label={t('pool.name')}
                    rules={[{ required: true, whitespace: true }]}
                  >
                    <Input autoFocus maxLength={255} />
                  </Form.Item>
                  <Row gutter={16}>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="target_size"
                        label={t('pool.prewarmedTarget')}
                        extra={t('pool.prewarmedTargetHint')}
                        rules={[{ required: true }]}
                      >
                        <InputNumber min={0} precision={0} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="max_total_members"
                        label={t('pool.maximumBillable')}
                        extra={t('pool.maximumBillableHint')}
                        dependencies={['target_size']}
                        rules={[
                          { required: true },
                          ({ getFieldValue }) => ({
                            validator(_, value) {
                              return value >= getFieldValue('target_size')
                                ? Promise.resolve()
                                : Promise.reject(
                                    new Error(t('pool.maximumBillableError')),
                                  )
                            },
                          }),
                        ]}
                      >
                        <InputNumber min={1} precision={0} />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Collapse
                    ghost
                    items={[
                      {
                        key: 'cost-protection',
                        label: t('pool.advancedCostProtection'),
                        children: (
                          <Row gutter={16}>
                            <Col xs={24} lg={8}>
                              <Form.Item
                                name="max_member_purchases_per_hour"
                                label={t('pool.purchaseBudget')}
                              >
                                <InputNumber min={1} precision={0} />
                              </Form.Item>
                            </Col>
                            <Col xs={24} lg={8}>
                              <Form.Item
                                name="max_create_requests_per_agent_per_hour"
                                label={t('pool.createBudget')}
                              >
                                <InputNumber min={1} precision={0} />
                              </Form.Item>
                            </Col>
                            <Col xs={24} lg={8}>
                              <Form.Item
                                name="max_delete_requests_per_agent_per_hour"
                                label={t('pool.deleteBudget')}
                              >
                                <InputNumber min={1} precision={0} />
                              </Form.Item>
                            </Col>
                          </Row>
                        ),
                      },
                    ]}
                  />
                </>
              )}

              {step === 2 && profile && (
                <>
                  <Title level={4}>{t('pool.networkTitle')}</Title>
                  <Paragraph type="secondary" className="dedicated-help-copy">
                    {t('pool.networkDescription')}
                  </Paragraph>
                  <Row gutter={16}>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="region_id"
                        label={t('pool.regionId')}
                        rules={[
                          { required: true },
                          { pattern: /^[a-z0-9-]+$/, message: t('pool.regionIdInvalid') },
                        ]}
                      >
                        <Input placeholder="cn-hangzhou" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="zone_id"
                        label={t('pool.zoneId')}
                        rules={[{ required: true }]}
                      >
                        <Input placeholder={t('pool.zoneIdPlaceholder')} />
                      </Form.Item>
                    </Col>
                  </Row>
                  {validRegion(regionId) && (
                    <Paragraph>
                      <a
                        href={`https://vpc.console.aliyun.com/vpc/${encodeURIComponent(regionId)}/vpcs`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {t('pool.openVpcConsole')} <ExportOutlined />
                      </a>
                    </Paragraph>
                  )}
                  <Row gutter={16}>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="vpc_id"
                        label={t('pool.vpcId')}
                        extra={t('pool.vpcHint')}
                        rules={[{ required: true }]}
                      >
                        <Input placeholder={t('pool.vpcIdPlaceholder')} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item
                        name="vswitch_id"
                        label={t('pool.vswitchId')}
                        extra={t('pool.vswitchHint')}
                        rules={[{ required: true }]}
                      >
                        <Input placeholder={t('pool.vswitchIdPlaceholder')} />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Form.Item
                    name="storage_type"
                    label={t('pool.storageType')}
                    extra={t('pool.storageTypeHint')}
                    rules={[{ required: true }]}
                  >
                    <Select
                      options={profile.supported_storage_types.map((value) => ({
                        value,
                        label: value,
                      }))}
                    />
                  </Form.Item>
                  <Collapse
                    ghost
                    items={[
                      {
                        key: 'data-plane-access',
                        label: t('pool.advancedDataPlaneAccess'),
                        children: (
                          <Form.Item
                            name="security_ip_list"
                            label={t('pool.pasPrivateSourceCidrs')}
                            extra={t('pool.pasPrivateSourceCidrsHint')}
                          >
                            <Input placeholder="10.0.0.0/24" />
                          </Form.Item>
                        ),
                      },
                    ]}
                  />
                  <Divider />
                  <Title level={5}>{t('pool.agenticSpecification')}</Title>
                  <Descriptions size="small" column={{ xs: 1, md: 2 }}>
                    <Descriptions.Item label={t('pool.databaseEngine')}>
                      {`${profile.fixed_parameters.db_type} ${profile.fixed_parameters.db_minor_version}`}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('pool.computeMode')}>
                      {`${profile.fixed_parameters.serverless_type} · ${profile.fixed_parameters.scale_min}–${profile.fixed_parameters.scale_max}`}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('pool.agentDatabase')}>
                      {t('pool.defaultAgenticName')}
                    </Descriptions.Item>
                    <Descriptions.Item label={t('pool.agentAccount')}>
                      {t('pool.defaultAgenticName')}
                    </Descriptions.Item>
                  </Descriptions>
                  <Collapse
                    ghost
                    items={[
                      {
                        key: 'purchase-profile',
                        label: t('pool.viewPurchaseParameters'),
                        children: (
                          <Descriptions
                            size="small"
                            bordered
                            column={{ xs: 1, md: 2 }}
                          >
                            {Object.entries(profile.fixed_parameters).map(
                              ([key, value]) => (
                                <Descriptions.Item key={key} label={key}>
                                  {value}
                                </Descriptions.Item>
                              ),
                            )}
                          </Descriptions>
                        ),
                      },
                    ]}
                  />
                </>
              )}

              {step === 3 && (
                <>
                  <Title level={4}>{t('pool.agentAccessTitle')}</Title>
                  <Form.Item
                    name="permission_template_revision_id"
                    label={t('pool.agentDefaultPermissions')}
                    extra={t('pool.agentDefaultPermissionsDescription')}
                    rules={[{ required: true }]}
                  >
                    <Select options={revisionOptions} />
                  </Form.Item>
                  {selectedRevision && (
                    <div className="permission-summary">
                      <Text strong>{t('pool.resolvedPermissions')}</Text>
                      <Space wrap>
                        {selectedRevision.privileges.map((privilege) => (
                          <Tag key={privilege}>{privilege}</Tag>
                        ))}
                        <Tag color={selectedRevision.grant_option ? 'warning' : 'default'}>
                          {selectedRevision.grant_option
                            ? t('pool.grantOptionEnabled')
                            : t('pool.grantOptionDisabled')}
                        </Tag>
                      </Space>
                    </div>
                  )}
                  <Form.Item
                    name="reclaim_policy"
                    label={t('pool.reclaimPolicy')}
                    extra={t('pool.reclaimPolicyHint')}
                  >
                    <Select
                      options={[
                        { value: 'destroy', label: t('pool.destroyAfterCooldown') },
                        {
                          value: 'sanitize_and_reuse',
                          label: t('pool.sanitizeReuse'),
                        },
                      ]}
                    />
                  </Form.Item>
                  <Alert
                    type="info"
                    showIcon
                    message={t('pool.pasLifecycleTitle')}
                    description={t('pool.pasLifecycleDescription')}
                  />
                  <Collapse
                    ghost
                    items={[
                      {
                        key: 'lifecycle',
                        label: t('pool.advancedLifecycle'),
                        children: (
                          <>
                            <Form.Item
                              name="delete_cooldown_duration_hours"
                              label={t('pool.defaultCooldown')}
                              extra={t('pool.cooldownInheritanceHint')}
                            >
                              <InputNumber
                                min={1}
                                precision={0}
                                placeholder="24"
                              />
                            </Form.Item>
                            <Row gutter={16}>
                              <Col xs={24} md={12}>
                                <Form.Item
                                  name="available_health_check_interval_seconds"
                                  label={t('pool.healthInterval')}
                                >
                                  <InputNumber min={1} precision={0} />
                                </Form.Item>
                              </Col>
                              <Col xs={24} md={12}>
                                <Form.Item
                                  name="available_health_stale_after_seconds"
                                  label={t('pool.healthMaxAge')}
                                  dependencies={[
                                    'available_health_check_interval_seconds',
                                  ]}
                                  rules={[
                                    ({ getFieldValue }) => ({
                                      validator(_, value) {
                                        const interval = getFieldValue(
                                          'available_health_check_interval_seconds',
                                        )
                                        return value >= 2 * interval
                                          ? Promise.resolve()
                                          : Promise.reject(
                                              new Error(t('pool.healthMaxAgeHint')),
                                            )
                                      },
                                    }),
                                  ]}
                                >
                                  <InputNumber min={2} precision={0} />
                                </Form.Item>
                              </Col>
                            </Row>
                          </>
                        ),
                      },
                    ]}
                  />
                </>
              )}
            </section>

            <div className="dedicated-create-actions">
              <Button
                onClick={() => {
                  if (step === 0) navigate('/pool')
                  else setStep((value) => value - 1)
                }}
              >
                {step === 0 ? t('common.cancel') : t('common.back')}
              </Button>
              {step < 3 ? (
                <Button type="primary" onClick={() => void next()}>
                  {t('common.next')}
                </Button>
              ) : (
                <Button type="primary" htmlType="submit" loading={submitting}>
                  {canPrewarm
                    ? t('pool.createAndPrewarm')
                    : t('pool.saveNotStarted')}
                </Button>
              )}
            </div>
          </Form>
        )}
      </div>
    </PageContainer>
  )
}
