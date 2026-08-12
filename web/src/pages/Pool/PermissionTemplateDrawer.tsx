import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Popconfirm,
  Select,
  Skeleton,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import { useTranslation } from 'react-i18next'

import { getAPIErrorMessage } from '../../api/client'
import {
  createPermissionTemplateRevision,
  listPermissionTemplates,
  requestPermissionSync,
  type PermissionSyncJob,
  type PermissionTemplate,
} from '../../api/permissionTemplates'
import {
  updateDedicatedPool,
  type DedicatedPool,
} from '../../api/dedicatedPools'

const { Paragraph, Text, Title } = Typography

interface PermissionTemplateDrawerProps {
  open: boolean
  poolId: string
  configRevision: number
  selectedRevisionId: string
  onPoolUpdated?: (pool: DedicatedPool) => void
  onClose: () => void
}

interface RevisionFormValues {
  privileges: string[]
  grant_option: boolean
}

const PRIVILEGES = [
  'CREATE',
  'DROP',
  'ALTER',
  'INDEX',
  'REFERENCES',
  'CREATE VIEW',
  'SHOW VIEW',
  'CREATE ROUTINE',
  'ALTER ROUTINE',
  'EXECUTE',
  'SELECT',
  'INSERT',
  'UPDATE',
  'DELETE',
  'CREATE TEMPORARY TABLES',
  'LOCK TABLES',
  'CREATE USER',
]

export default function PermissionTemplateDrawer({
  open,
  poolId,
  configRevision,
  selectedRevisionId,
  onPoolUpdated,
  onClose,
}: PermissionTemplateDrawerProps) {
  const { t } = useTranslation()
  const [revisionForm] = Form.useForm<RevisionFormValues>()
  const [templates, setTemplates] = useState<PermissionTemplate[]>([])
  const [revisionId, setRevisionId] = useState(selectedRevisionId)
  const [savedRevisionId, setSavedRevisionId] = useState(selectedRevisionId)
  const [preview, setPreview] = useState<PermissionSyncJob | null>(null)
  const [loading, setLoading] = useState(false)
  const [savingDefault, setSavingDefault] = useState(false)
  const [creatingRevision, setCreatingRevision] = useState(false)
  const [revisionSubmitting, setRevisionSubmitting] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setRevisionId(selectedRevisionId)
    setSavedRevisionId(selectedRevisionId)
    setPreview(null)
    setCreatingRevision(false)
    setError(null)
    setLoading(true)
    void listPermissionTemplates()
      .then((response) => setTemplates(response.data))
      .catch((requestError) =>
        setError(
          getAPIErrorMessage(
            requestError,
            t('pool.permissionLoadFailed'),
          ),
        ),
      )
      .finally(() => setLoading(false))
  }, [open, selectedRevisionId, t])

  const revisions = useMemo(
    () =>
      templates.flatMap((template) =>
        template.revisions.map((revision) => ({
          value: revision.id,
          label: `${template.name} · v${revision.revision}`,
          template,
          revision,
        })),
      ),
    [templates],
  )
  const selected = revisions.find((item) => item.value === revisionId)
  const changedCount =
    preview?.targets.filter((target) => target.change_required).length ?? 0

  const saveDefault = async () => {
    if (!revisionId || revisionId === savedRevisionId) return
    setSavingDefault(true)
    setError(null)
    try {
      const response = await updateDedicatedPool(poolId, {
        expected_config_revision: configRevision,
        permission_template_revision_id: revisionId,
      })
      setSavedRevisionId(revisionId)
      onPoolUpdated?.(response.data)
      message.success(t('pool.permissionDefaultUpdated'))
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('pool.permissionDefaultUpdateFailed')),
      )
    } finally {
      setSavingDefault(false)
    }
  }

  const startRevision = () => {
    if (!selected) return
    revisionForm.setFieldsValue({
      privileges: selected.revision.privileges,
      grant_option: selected.revision.grant_option,
    })
    setCreatingRevision(true)
  }

  const createRevision = async () => {
    if (!selected) return
    let values: RevisionFormValues
    try {
      values = await revisionForm.validateFields()
    } catch {
      return
    }
    setRevisionSubmitting(true)
    setError(null)
    try {
      const response = await createPermissionTemplateRevision(
        selected.template.id,
        values,
      )
      const revision = response.data
      setTemplates((current) => current.map((template) =>
        template.id === selected.template.id
          ? { ...template, revisions: [revision, ...template.revisions] }
          : template,
      ))
      setRevisionId(revision.id)
      setPreview(null)
      setCreatingRevision(false)
      message.success(t('pool.permissionVersionCreated'))
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('pool.permissionVersionCreateFailed')),
      )
    } finally {
      setRevisionSubmitting(false)
    }
  }

  const runPreview = async () => {
    if (!revisionId) return
    setPreviewing(true)
    setError(null)
    try {
      const response = await requestPermissionSync(revisionId, {
        target_scope: 'pool',
        target_id: poolId,
        mode: 'dry_run',
        confirmed: false,
      })
      setPreview(response.data)
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('pool.permissionPreviewFailed')),
      )
    } finally {
      setPreviewing(false)
    }
  }

  const apply = async () => {
    if (!revisionId || !preview) return
    setApplying(true)
    setError(null)
    try {
      await requestPermissionSync(revisionId, {
        target_scope: 'pool',
        target_id: poolId,
        mode: 'apply',
        confirmed: true,
      })
      message.success(t('pool.permissionSyncQueued'))
      onClose()
    } catch (requestError) {
      setError(
        getAPIErrorMessage(requestError, t('pool.permissionApplyFailed')),
      )
    } finally {
      setApplying(false)
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={560}
      title={t('pool.permissionTitle')}
      destroyOnClose
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : revisions.length === 0 ? (
        <Empty description={t('pool.permissionEmpty')} />
      ) : (
        <Space direction="vertical" size={20} style={{ width: '100%' }}>
          {error && <Alert type="error" showIcon message={error} />}
          <div>
            <Title level={5}>{t('pool.permissionRevision')}</Title>
            <Select
              aria-label={t('pool.permissionRevision')}
              style={{ width: '100%' }}
              value={revisionId || undefined}
              options={revisions.map(({ value, label }) => ({ value, label }))}
              onChange={(value) => {
                setRevisionId(value)
                setPreview(null)
              }}
            />
          </div>
          {selected && (
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label={t('pool.permissionPrivileges')}>
                <Space size={[4, 4]} wrap>
                  {selected.revision.privileges.map((privilege) => (
                    <Tag key={privilege}>{privilege}</Tag>
                  ))}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label={t('pool.permissionGrantOption')}>
                {selected.revision.grant_option
                  ? t('common.enabled')
                  : t('common.disabled')}
              </Descriptions.Item>
            </Descriptions>
          )}
          <Space wrap>
            <Button
              type="primary"
              loading={savingDefault}
              disabled={!revisionId || revisionId === savedRevisionId}
              onClick={() => void saveDefault()}
            >
              {t('pool.permissionUseForFuture')}
            </Button>
            <Button onClick={startRevision}>
              {t('pool.permissionCreateVersion')}
            </Button>
          </Space>
          {creatingRevision && selected && (
            <Form<RevisionFormValues>
              form={revisionForm}
              layout="vertical"
              onFinish={() => void createRevision()}
            >
              <Alert
                type="info"
                showIcon
                message={t('pool.permissionImmutableVersionTitle')}
                description={t('pool.permissionImmutableVersionDescription')}
              />
              <Form.Item
                name="privileges"
                label={t('pool.permissionPrivileges')}
                rules={[{ required: true, message: t('pool.permissionPrivilegesRequired') }]}
                style={{ marginTop: 16 }}
              >
                <Checkbox.Group
                  options={PRIVILEGES.map((value) => ({ label: value, value }))}
                />
              </Form.Item>
              <Form.Item name="grant_option" valuePropName="checked">
                <Checkbox>{t('pool.permissionGrantOption')}</Checkbox>
              </Form.Item>
              <Alert
                type="warning"
                showIcon
                message={t('pool.permissionElevatedWarning')}
              />
              <Space style={{ marginTop: 16 }}>
                <Button
                  type="primary"
                  htmlType="submit"
                  loading={revisionSubmitting}
                >
                  {t('pool.permissionCreateVersionSubmit')}
                </Button>
                <Button onClick={() => setCreatingRevision(false)}>
                  {t('common.cancel')}
                </Button>
              </Space>
            </Form>
          )}
          <Divider />
          <Title level={5}>{t('pool.permissionExistingAccountsTitle')}</Title>
          <Alert
            type="info"
            showIcon
            message={t('pool.permissionSnapshotTitle')}
            description={t('pool.permissionSnapshotDescription')}
          />
          <Button loading={previewing} onClick={() => void runPreview()}>
            {t('pool.permissionPreview')}
          </Button>
          {preview && (
            <section aria-live="polite">
              <Title level={5}>{t('pool.permissionPreviewResult')}</Title>
              <Paragraph>
                {t('pool.permissionChangeCount', { count: changedCount })}
              </Paragraph>
              <Text type="secondary">
                {t('pool.permissionTargetCount', {
                  count: preview.total_count,
                })}
              </Text>
              {preview.total_count === 0 ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginTop: 16 }}
                  message={t('pool.permissionNoExistingAccounts')}
                  description={t('pool.permissionNoExistingAccountsDescription')}
                />
              ) : (
                <div style={{ marginTop: 16 }}>
                <Popconfirm
                  title={t('pool.permissionConfirmTitle')}
                  description={t('pool.permissionConfirmDescription')}
                  okText={t('pool.permissionConfirm')}
                  cancelText={t('common.cancel')}
                  onConfirm={() => void apply()}
                >
                  <Button
                    type="primary"
                    loading={applying}
                    disabled={changedCount === 0}
                  >
                    {t('pool.permissionApply')}
                  </Button>
                </Popconfirm>
                </div>
              )}
            </section>
          )}
        </Space>
      )}
    </Drawer>
  )
}
