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
import {
  getDashboardStats,
  type DashboardStats,
  type DashboardViewStats,
  type MemberDashboardStats,
} from '../../api/dashboard'
import './Dashboard.css'

interface DashboardProps {
  isAdmin: boolean
}

export default function Dashboard({ isAdmin }: DashboardProps) {
  const [stats, setStats] = useState<DashboardViewStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const { t } = useTranslation()

  useEffect(() => {
    getDashboardStats(isAdmin)
      .then(setStats)
      .catch(() => setError(t('dashboard.loadFailed')))
      .finally(() => setLoading(false))
  }, [isAdmin, t])

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

  const adminStats = stats as DashboardStats | null
  const memberStats = stats as MemberDashboardStats | null
  const statCards = isAdmin
    ? [
        { icon: <TeamOutlined />, color: 'blue', value: adminStats?.total_users ?? 0, label: t('dashboard.totalUsers') },
        { icon: <DatabaseOutlined />, color: 'purple', value: adminStats?.total_instances ?? 0, label: t('dashboard.instances') },
        { icon: <CheckCircleOutlined />, color: 'green', value: adminStats?.active_instances ?? 0, label: t('dashboard.active') },
        { icon: <CloudOutlined />, color: 'cyan', value: adminStats?.dedicated_allocatable ?? 0, label: t('dashboard.dedicatedAllocatable') },
        { icon: <ApartmentOutlined />, color: 'orange', value: adminStats?.departments ?? 0, label: t('dashboard.departments') },
        { icon: <FileTextOutlined />, color: 'red', value: adminStats?.queries_today ?? 0, label: t('dashboard.queriesToday') },
      ]
    : [
        { icon: <DatabaseOutlined />, color: 'purple', value: memberStats?.database_instances ?? 0, label: t('dashboard.databaseInstances') },
        { icon: <FileTextOutlined />, color: 'cyan', value: memberStats?.knowledge_resources ?? 0, label: t('dashboard.knowledgeResources') },
      ]

  const quickActions = isAdmin
    ? [
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
          title: t('dashboard.serviceConfiguration'),
          desc: t('dashboard.serviceConfigurationDescription'),
          path: '/settings',
        },
      ]
    : [
        {
          icon: <DatabaseOutlined />,
          iconBg: 'rgba(0, 113, 227, 0.1)',
          iconColor: '#0071e3',
          title: t('dashboard.viewMyInstances'),
          desc: t('dashboard.viewMyInstancesDescription'),
          path: '/my-instances',
        },
      ]

  return (
    <div className="page-enter">
      <div style={{ marginBottom: 24 }}>
        <h2 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px', letterSpacing: '-0.02em' }}>{t('dashboard.title')}</h2>
        <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0 }}>
          {isAdmin
            ? t('dashboard.adminDescription')
            : t('dashboard.memberDescription')}
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
