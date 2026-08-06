import { Alert, Checkbox, Form, Input, InputNumber, Select, Space, Typography } from 'antd'
import { useTranslation } from 'react-i18next'
import type { ConfigModule, JSONSchemaProperty } from '../../api/configuration'
import { schemaFieldLabel } from '../../i18n/schema'

interface Props {
  module: ConfigModule
  disabled?: boolean
  onSubmit: (values: Record<string, unknown>) => void | Promise<void>
  onValuesChange?: () => void
  formId?: string
}

function schemaType(schema: JSONSchemaProperty): string {
  if (schema.type) return schema.type
  return schema.anyOf?.find((item) => item.type && item.type !== 'null')?.type ?? 'string'
}

function trimStrings(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).map(([name, value]) => {
      if (name === 'password') return [name, value]
      if (typeof value === 'string') return [name, value.trim()]
      if (Array.isArray(value)) {
        return [name, value.map((item) => (typeof item === 'string' ? item.trim() : item))]
      }
      return [name, value]
    }),
  )
}

export default function ConfigModuleForm({
  module,
  disabled,
  onSubmit,
  onValuesChange,
  formId = `config-form-${module.name}`,
}: Props) {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  const properties = module.schema.properties ?? {}
  const required = new Set(module.schema.required ?? [])
  const secretFields = new Set(module.ui_hints?.secret_fields ?? [])
  const docs = module.ui_hints?.docs ?? []
  const storedValues = { ...(module.effective?.config ?? {}), ...(module.draft ?? {}) }
  const initialValues = {
    ...Object.fromEntries(
      Object.entries(properties)
        .filter(([, schema]) => schema.default !== undefined)
        .map(([name, schema]) => [name, schema.default]),
    ),
    ...storedValues,
  }
  for (const field of secretFields) {
    delete initialValues[field]
  }

  return (
    <Form
      id={formId}
      form={form}
      layout="vertical"
      initialValues={initialValues}
      disabled={disabled}
      onFinish={(values) => onSubmit(trimStrings(values))}
      onValuesChange={onValuesChange}
      requiredMark="optional"
      aria-label={t('components.configuration.formLabel', { module: module.name })}
    >
      {docs.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space direction="vertical" size={4}>
              {docs.map((doc) => (
                <span key={doc.url}>
                  <Typography.Link href={doc.url} target="_blank" rel="noopener noreferrer">
                    {doc.label}
                  </Typography.Link>
                  {doc.description && (
                    <Typography.Text type="secondary"> — {doc.description}</Typography.Text>
                  )}
                </span>
              ))}
            </Space>
          }
        />
      )}
      {Object.entries(properties).map(([name, schema]) => {
        const label = schemaFieldLabel(
          t,
          module.name,
          name,
          schema.title ?? name.replace(/_/g, ' '),
        )
        const configured = secretFields.has(name) && storedValues[name] != null
        const rules = [
          ...(required.has(name) && !configured
            ? [{ required: true, message: t('components.configuration.required', { label }) }]
            : []),
          ...(schema.minLength !== undefined
            ? [{
                min: schema.minLength,
                message: t('components.configuration.minLength', { label, count: schema.minLength }),
              }]
            : []),
          ...(schema.maxLength !== undefined
            ? [{
                max: schema.maxLength,
                message: t('components.configuration.maxLength', { label, count: schema.maxLength }),
              }]
            : []),
        ]
        const type = schemaType(schema)
        let control
        if (schema.enum) {
          control = (
            <Select
              options={schema.enum.map((value) => ({
                value,
                label: String(value),
              }))}
            />
          )
        } else if (type === 'boolean') {
          control = <Checkbox>{schema.description}</Checkbox>
        } else if (type === 'integer' || type === 'number') {
          control = <InputNumber min={schema.minimum} max={schema.maximum} style={{ width: '100%' }} />
        } else if (type === 'array') {
          control = <Select mode="tags" tokenSeparators={[',']} />
        } else if (secretFields.has(name)) {
          control = (
            <Input.Password
              autoComplete="new-password"
              placeholder={configured ? t('components.configuration.configuredPlaceholder') : t('components.configuration.secretPlaceholder')}
            />
          )
        } else {
          control = <Input />
        }
        return (
          <Form.Item
            key={name}
            name={name}
            label={label}
            valuePropName={type === 'boolean' ? 'checked' : 'value'}
            rules={rules}
            extra={
              secretFields.has(name) ? (
                <Space size={6}>
                  <Typography.Text type="secondary">
                    {configured ? t('components.configuration.configuredSecret') : t('components.configuration.encryptedSecret')}
                  </Typography.Text>
                </Space>
              ) : (
                schema.description
              )
            }
          >
            {control}
          </Form.Item>
        )
      })}
      {Object.keys(properties).length === 0 && (
        <Alert type="info" showIcon message={t('components.configuration.noFields')} />
      )}
    </Form>
  )
}
