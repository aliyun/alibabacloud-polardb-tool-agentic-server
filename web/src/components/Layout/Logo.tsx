import { DatabaseOutlined } from '@ant-design/icons'

interface LogoProps {
  collapsed?: boolean
}

export default function Logo({ collapsed }: LogoProps) {
  return (
    <div className="sidebar-logo">
      <div className="sidebar-logo-icon">
        <DatabaseOutlined />
      </div>
      {!collapsed && <span className="sidebar-logo-text">PolarDB Agentic</span>}
    </div>
  )
}
