import type { ReactNode } from 'react'
import { Typography, Space } from 'antd'
import './PageContainer.css'

const { Title, Text } = Typography

interface PageContainerProps {
  title: string
  description?: string
  actions?: ReactNode
  children: ReactNode
}

export default function PageContainer({ title, description, actions, children }: PageContainerProps) {
  return (
    <div className="page-container page-enter">
      <div className="page-container-header">
        <div className="page-container-header-left">
          <Title level={3}>{title}</Title>
          {description && (
            <Text type="secondary" style={{ marginTop: 4, display: 'block' }}>
              {description}
            </Text>
          )}
        </div>
        {actions && <Space>{actions}</Space>}
      </div>
      <div className="page-container-body">
        {children}
      </div>
    </div>
  )
}
