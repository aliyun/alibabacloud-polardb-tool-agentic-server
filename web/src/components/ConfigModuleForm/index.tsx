import { Alert, Checkbox, Form, Input, InputNumber, Select, Space, Typography } from 'antd'
import type { ConfigModule, JSONSchemaProperty } from '../../api/configuration'

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
      aria-label={`${module.name} configuration`}
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
        const label = schema.title ?? name.replace(/_/g, ' ')
        const configured = secretFields.has(name) && storedValues[name] != null
        const rules = [
          ...(required.has(name) && !configured
            ? [{ required: true, message: `${label} is required` }]
            : []),
          ...(schema.minLength !== undefined
            ? [{
                min: schema.minLength,
                message: `${label} must be at least ${schema.minLength} characters`,
              }]
            : []),
          ...(schema.maxLength !== undefined
            ? [{
                max: schema.maxLength,
                message: `${label} must be at most ${schema.maxLength} characters`,
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
              placeholder={configured ? 'Configured — leave blank to keep' : 'Enter secret'}
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
                    {configured ? 'A secret is configured. Its value is never returned.' : 'Stored encrypted after save.'}
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
        <Alert type="info" showIcon message="This module uses its safe defaults and has no editable fields." />
      )}
    </Form>
  )
}
