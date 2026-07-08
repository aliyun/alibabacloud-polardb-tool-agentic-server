import { useEffect, useState } from 'react'
import { Spin } from 'antd'
import {
  TeamOutlined,
  DatabaseOutlined,
  CheckCircleOutlined,
  CloudOutlined,
  ApartmentOutlined,
  FileTextOutlined,
  PlusOutlined,
  UserAddOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { getDashboardStats, type DashboardStats } from '../../api/dashboard'
import './Dashboard.css'

export default function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    getDashboardStats()
      .then(setStats)
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  const statCards = [
    { icon: <TeamOutlined />, color: 'blue', value: stats?.total_users ?? 0, label: 'Total Users' },
    { icon: <DatabaseOutlined />, color: 'purple', value: stats?.total_instances ?? 0, label: 'Instances' },
    { icon: <CheckCircleOutlined />, color: 'green', value: stats?.active_instances ?? 0, label: 'Active' },
    { icon: <CloudOutlined />, color: 'cyan', value: stats?.pool_available ?? 0, label: 'Pool Available' },
    { icon: <ApartmentOutlined />, color: 'orange', value: stats?.departments ?? 0, label: 'Departments' },
    { icon: <FileTextOutlined />, color: 'red', value: stats?.queries_today ?? 0, label: 'Queries Today' },
  ]

  const quickActions = [
    {
      icon: <PlusOutlined />,
      iconBg: 'rgba(0, 113, 227, 0.1)',
      iconColor: '#0071e3',
      title: 'Register Instance',
      desc: 'Add a new PolarDB cluster to manage',
      path: '/instances',
    },
    {
      icon: <UserAddOutlined />,
      iconBg: 'rgba(52, 199, 89, 0.1)',
      iconColor: '#34c759',
      title: 'Manage Users',
      desc: 'Add users and assign permissions',
      path: '/users',
    },
    {
      icon: <SearchOutlined />,
      iconBg: 'rgba(175, 82, 222, 0.1)',
      iconColor: '#af52de',
      title: 'View Audit Logs',
      desc: 'Review system activity and SQL queries',
      path: '/audit-logs',
    },
    {
      icon: <SettingOutlined />,
      iconBg: 'rgba(255, 159, 10, 0.1)',
      iconColor: '#ff9f0a',
      title: 'System Settings',
      desc: 'Configure pool, quotas, and provisioning',
      path: '/settings',
    },
  ]

  return (
    <div className="page-enter">
      <div style={{ marginBottom: 24 }}>
        <h2 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px', letterSpacing: '-0.02em' }}>Dashboard</h2>
        <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>
          Overview of your PolarDB Agentic environment
        </p>
      </div>

      {/* Stats */}
      <div className="dashboard-stats">
        {statCards.map((card) => (
          <div className="stat-card" key={card.label}>
            <div className={`stat-card-icon ${card.color}`}>{card.icon}</div>
            <div className="stat-card-body">
              <div className="stat-card-value">{card.value}</div>
              <div className="stat-card-label">{card.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Quick Actions */}
      <h3 className="dashboard-section-title">Quick Actions</h3>
      <div className="quick-actions">
        {quickActions.map((action) => (
          <div
            className="quick-action-card"
            key={action.title}
            onClick={() => navigate(action.path)}
          >
            <div
              className="quick-action-icon"
              style={{ background: action.iconBg, color: action.iconColor }}
            >
              {action.icon}
            </div>
            <div className="quick-action-content">
              <div className="quick-action-title">{action.title}</div>
              <div className="quick-action-desc">{action.desc}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
