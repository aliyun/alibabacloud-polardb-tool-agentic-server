import { useEffect, useState } from 'react'
import { Alert, Spin } from 'antd'
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
import { useTranslation } from 'react-i18next'
import { getDashboardStats, type DashboardStats } from '../../api/dashboard'
import './Dashboard.css'

export default function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const { t } = useTranslation()

  useEffect(() => {
    getDashboardStats()
      .then(setStats)
      .catch(() => setError(t('dashboard.loadFailed')))
      .finally(() => setLoading(false))
  }, [t])

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }
  if (error) {
    return <Alert type="error" showIcon message={error} />
  }

  const statCards = [
    { icon: <TeamOutlined />, color: 'blue', value: stats?.total_users ?? 0, label: t('dashboard.totalUsers') },
    { icon: <DatabaseOutlined />, color: 'purple', value: stats?.total_instances ?? 0, label: t('dashboard.instances') },
    { icon: <CheckCircleOutlined />, color: 'green', value: stats?.active_instances ?? 0, label: t('dashboard.active') },
    { icon: <CloudOutlined />, color: 'cyan', value: stats?.pool_available ?? 0, label: t('dashboard.poolAvailable') },
    { icon: <ApartmentOutlined />, color: 'orange', value: stats?.departments ?? 0, label: t('dashboard.departments') },
    { icon: <FileTextOutlined />, color: 'red', value: stats?.queries_today ?? 0, label: t('dashboard.queriesToday') },
  ]

  const quickActions = [
    {
      icon: <PlusOutlined />,
      iconBg: 'rgba(0, 113, 227, 0.1)',
      iconColor: '#0071e3',
      title: t('dashboard.registerInstance'),
      desc: t('dashboard.registerInstanceDescription'),
      path: '/instances',
    },
    {
      icon: <UserAddOutlined />,
      iconBg: 'rgba(52, 199, 89, 0.1)',
      iconColor: '#34c759',
      title: t('dashboard.manageUsers'),
      desc: t('dashboard.manageUsersDescription'),
      path: '/users',
    },
    {
      icon: <SearchOutlined />,
      iconBg: 'rgba(175, 82, 222, 0.1)',
      iconColor: '#af52de',
      title: t('dashboard.viewAuditLogs'),
      desc: t('dashboard.viewAuditLogsDescription'),
      path: '/audit-logs',
    },
    {
      icon: <SettingOutlined />,
      iconBg: 'rgba(255, 159, 10, 0.1)',
      iconColor: '#ff9f0a',
      title: t('dashboard.systemSettings'),
      desc: t('dashboard.systemSettingsDescription'),
      path: '/settings',
    },
  ]

  return (
    <div className="page-enter">
      <div style={{ marginBottom: 24 }}>
        <h2 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px', letterSpacing: '-0.02em' }}>{t('dashboard.title')}</h2>
        <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>
          {t('dashboard.description')}
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
      <h3 className="dashboard-section-title">{t('dashboard.quickActions')}</h3>
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
