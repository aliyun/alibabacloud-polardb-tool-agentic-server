import { useState } from 'react'
import { Layout as AntLayout, Menu, Modal, Form, Input, message, Dropdown } from 'antd'
import {
  DashboardOutlined,
  TeamOutlined,
  RobotOutlined,
  ApartmentOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  FileTextOutlined,
  SettingOutlined,
  CloudOutlined,
  LockOutlined,
  LogoutOutlined,
} from '@ant-design/icons'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { UserInfo } from '../../hooks/useAuth'
import api, { getAPIErrorMessage } from '../../api/client'
import Logo from './Logo'
import LanguageSwitcher from '../LanguageSwitcher'
import './Layout.css'

const { Header, Sider, Content } = AntLayout

interface LayoutProps {
  user: UserInfo
  onLogout: () => void
  authMode: string
}

export default function AppLayout({ user, onLogout, authMode }: LayoutProps) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const location = useLocation()
  const isAdmin = user.role === 'admin'
  const selectedMenuKey = location.pathname.startsWith('/agents/')
    ? '/agents'
    : location.pathname
  const [collapsed, setCollapsed] = useState(false)
  const [pwdModalOpen, setPwdModalOpen] = useState(false)
  const [pwdLoading, setPwdLoading] = useState(false)
  const [pwdForm] = Form.useForm()

  const menuItems = [
    { key: '/dashboard', icon: <DashboardOutlined />, label: t('layout.dashboard') },
    ...(isAdmin
      ? [
          { key: '/users', icon: <TeamOutlined />, label: t('layout.users') },
          { key: '/departments', icon: <ApartmentOutlined />, label: t('layout.departments') },
          { key: '/instances', icon: <DatabaseOutlined />, label: t('layout.instances') },
          { key: '/agents', icon: <RobotOutlined />, label: t('layout.agents') },
        ]
      : []),
    { key: '/my-instances', icon: <CloudServerOutlined />, label: t('layout.myInstances') },
    ...(isAdmin
      ? [
          { key: '/audit-logs', icon: <FileTextOutlined />, label: t('layout.auditLogs') },
          {
            key: '/settings/configuration',
            icon: <SettingOutlined />,
            label: t('layout.configuration'),
          },
          { key: '/settings', icon: <SettingOutlined />, label: t('layout.settings') },
          { key: '/pool', icon: <CloudOutlined />, label: t('layout.pool') },
        ]
      : []),
  ]

  const handleChangePassword = async (values: { current_password: string; new_password: string }) => {
    setPwdLoading(true)
    try {
      await api.post('/auth/change-password', values)
      message.success(t('layout.passwordChanged'))
      setPwdModalOpen(false)
      pwdForm.resetFields()
    } catch (error: unknown) {
      message.error(
        getAPIErrorMessage(error, t('layout.passwordChangeFailed')),
      )
    } finally {
      setPwdLoading(false)
    }
  }

  const userMenuItems = [
    ...(authMode === 'builtin'
      ? [{ key: 'change-password', icon: <LockOutlined />, label: t('layout.changePassword') }]
      : []),
    { key: 'logout', icon: <LogoutOutlined />, label: t('layout.logout') },
  ]

  // Get user initials for avatar
  const initials = user.display_name
    ? user.display_name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2)
    : 'U'

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <Sider
        className="app-sidebar"
        width={240}
        collapsedWidth={64}
        collapsed={collapsed}
        onCollapse={setCollapsed}
        collapsible
        trigger={null}
      >
        <Logo collapsed={collapsed} />
        <Menu
          mode="inline"
          selectedKeys={[selectedMenuKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <AntLayout>
        <Header className="app-header">
          <LanguageSwitcher />
          <Dropdown
            menu={{
              items: userMenuItems,
              onClick: ({ key }) => {
                if (key === 'change-password') setPwdModalOpen(true)
                else if (key === 'logout') onLogout()
              },
            }}
          >
            <div className="user-pill">
              <div className="user-pill-avatar">{initials}</div>
              <span className="user-pill-name">{user.display_name}</span>
            </div>
          </Dropdown>
        </Header>
        <Content className="app-content">
          <div className="app-content-inner">
            <Outlet />
          </div>
        </Content>
      </AntLayout>

      <Modal
        title={t('layout.changePassword')}
        open={pwdModalOpen}
        onCancel={() => { setPwdModalOpen(false); pwdForm.resetFields() }}
        onOk={() => pwdForm.submit()}
        confirmLoading={pwdLoading}
      >
        <Form form={pwdForm} layout="vertical" onFinish={handleChangePassword}>
          <Form.Item name="current_password" label={t('layout.currentPassword')} rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="new_password" label={t('layout.newPassword')} rules={[{ required: true, min: 8, message: t('layout.passwordMinimum') }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label={t('layout.confirmPassword')}
            dependencies={['new_password']}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                  return Promise.reject(new Error(t('layout.passwordsMismatch')))
                },
              }),
            ]}
          >
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </AntLayout>
  )
}
