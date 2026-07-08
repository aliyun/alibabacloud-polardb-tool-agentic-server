import { useState } from 'react'
import { Layout as AntLayout, Menu, Modal, Form, Input, message, Dropdown } from 'antd'
import {
  DashboardOutlined,
  TeamOutlined,
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
import type { UserInfo } from '../../hooks/useAuth'
import api from '../../api/client'
import Logo from './Logo'
import './Layout.css'

const { Header, Sider, Content } = AntLayout

interface LayoutProps {
  user: UserInfo
  onLogout: () => void
  authMode: string
}

export default function AppLayout({ user, onLogout, authMode }: LayoutProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const isAdmin = user.role === 'admin'
  const [collapsed, setCollapsed] = useState(false)
  const [pwdModalOpen, setPwdModalOpen] = useState(false)
  const [pwdLoading, setPwdLoading] = useState(false)
  const [pwdForm] = Form.useForm()

  const menuItems = [
    { key: '/dashboard', icon: <DashboardOutlined />, label: 'Dashboard' },
    ...(isAdmin
      ? [
          { key: '/users', icon: <TeamOutlined />, label: 'Users' },
          { key: '/departments', icon: <ApartmentOutlined />, label: 'Departments' },
          { key: '/instances', icon: <DatabaseOutlined />, label: 'Instances' },
        ]
      : []),
    { key: '/my-instances', icon: <CloudServerOutlined />, label: 'My Instances' },
    ...(isAdmin
      ? [
          { key: '/audit-logs', icon: <FileTextOutlined />, label: 'Audit Logs' },
          { key: '/settings', icon: <SettingOutlined />, label: 'Settings' },
          { key: '/pool', icon: <CloudOutlined />, label: 'Pool' },
        ]
      : []),
  ]

  const handleChangePassword = async (values: { current_password: string; new_password: string }) => {
    setPwdLoading(true)
    try {
      await api.post('/auth/change-password', values)
      message.success('Password changed successfully')
      setPwdModalOpen(false)
      pwdForm.resetFields()
    } catch (err: any) {
      message.error(err.response?.data?.detail || 'Failed to change password')
    } finally {
      setPwdLoading(false)
    }
  }

  const userMenuItems = [
    ...(authMode === 'builtin'
      ? [{ key: 'change-password', icon: <LockOutlined />, label: 'Change Password' }]
      : []),
    { key: 'logout', icon: <LogoutOutlined />, label: 'Logout' },
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
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <AntLayout>
        <Header className="app-header">
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
        title="Change Password"
        open={pwdModalOpen}
        onCancel={() => { setPwdModalOpen(false); pwdForm.resetFields() }}
        onOk={() => pwdForm.submit()}
        confirmLoading={pwdLoading}
      >
        <Form form={pwdForm} layout="vertical" onFinish={handleChangePassword}>
          <Form.Item name="current_password" label="Current Password" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="new_password" label="New Password" rules={[{ required: true, min: 8, message: 'At least 8 characters' }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="Confirm Password"
            dependencies={['new_password']}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) return Promise.resolve()
                  return Promise.reject(new Error('Passwords do not match'))
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
