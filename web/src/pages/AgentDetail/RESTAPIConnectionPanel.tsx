import { Button, Descriptions, Space, Typography } from 'antd'
import { ApiOutlined, GithubOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

const { Paragraph, Text, Title } = Typography

const GITHUB_GUIDE_BASE =
  'https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/blob/main/docs'

export interface RESTAPIConnectionPanelProps {
  serverBaseUrl: string
}

export default function RESTAPIConnectionPanel({
  serverBaseUrl,
}: RESTAPIConnectionPanelProps) {
  const { t, i18n } = useTranslation()
  const lifecycleUrl = `${serverBaseUrl}/mcp/rest/db-instances`
  const docsUrl = `${serverBaseUrl}/mcp/rest/docs`
  const openapiUrl = `${serverBaseUrl}/mcp/rest/openapi.json`
  const guideLocale = i18n.language.toLowerCase().startsWith('zh')
    ? 'zh-cn'
    : 'en'
  const guideUrl = `${GITHUB_GUIDE_BASE}/${guideLocale}/database-instances/agent-rest-provisioning.md`
  const curlExample = `curl -X POST '${lifecycleUrl}' \\
  -H 'Authorization: Bearer <agent-token>' \\
  -H 'Content-Type: application/json' \\
  -d '{"client_token":"job-018f7f2d","name":"orders-sandbox","db_type":"polardb_mysql","provisioning_mode":"dedicated"}'`

  return (
    <>
      <div>
        <Title id="agent-rest-api-heading" level={4} style={{ marginBlock: 0 }}>
          {t('components.restApiConnection.title')}
        </Title>
        <Text type="secondary">
          {t('components.restApiConnection.description')}
        </Text>
      </div>

      <Descriptions column={1} size="small" style={{ marginTop: 16 }}>
        <Descriptions.Item label={t('components.restApiConnection.baseUrl')}>
          <Text code copyable style={{ wordBreak: 'break-all' }}>
            {lifecycleUrl}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label={t('components.restApiConnection.authentication')}>
          <Text code>{'Authorization: Bearer <agent-token>'}</Text>
        </Descriptions.Item>
      </Descriptions>

      <Text strong>{t('components.restApiConnection.example')}</Text>
      <Paragraph
        copyable={{ text: curlExample }}
        style={{ marginTop: 8, marginBottom: 0 }}
      >
        <pre
          style={{
            margin: 0,
            padding: 12,
            borderRadius: 8,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            background: 'rgba(0, 0, 0, 0.04)',
          }}
        >
          <code>{curlExample}</code>
        </pre>
      </Paragraph>
      <Text type="secondary">
        {t('components.restApiConnection.tokenSafety')}
      </Text>

      <Space wrap style={{ marginTop: 16 }}>
        <Button
          type="primary"
          icon={<ApiOutlined />}
          href={docsUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('components.restApiConnection.openDocs')}
        </Button>
        <Button
          href={openapiUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('components.restApiConnection.openSchema')}
        </Button>
        <Button
          icon={<GithubOutlined />}
          href={guideUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('components.restApiConnection.openGuide')}
        </Button>
      </Space>

      <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
        {t('components.restApiConnection.authority')}
      </Paragraph>
    </>
  )
}
