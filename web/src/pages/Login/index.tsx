import { useState } from 'react'
import { Form, Input, Button, message } from 'antd'
import { UserOutlined, LockOutlined, ApiOutlined, CloudOutlined, TeamOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import './Login.css'

interface LoginProps {
  onLogin: (username: string, password: string) => Promise<void>
}

export default function Login({ onLogin }: LoginProps) {
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      await onLogin(values.username, values.password)
      navigate('/dashboard')
    } catch {
      message.error('Invalid username or password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      {/* Brand Panel */}
      <div className="login-brand">
        <div className="login-brand-content">
          <h1 className="login-brand-title">alibabacloud polardb tool agentic server</h1>
          <p className="login-brand-subtitle">
            The open-source MCP gateway for PolarDB MySQL.<br />
            AI-native database access with enterprise security.
          </p>

          <div className="login-features">
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <ApiOutlined style={{ color: '#5ac8fa' }} />
              </span>
              <div className="login-feature-title">MCP Protocol</div>
              <div className="login-feature-desc">AI-native database access via Model Context Protocol</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <CloudOutlined style={{ color: '#34c759' }} />
              </span>
              <div className="login-feature-title">Auto Provisioning</div>
              <div className="login-feature-desc">Zero-touch instance management and scaling</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <TeamOutlined style={{ color: '#af52de' }} />
              </span>
              <div className="login-feature-title">Multi-Tenant</div>
              <div className="login-feature-desc">Secure isolation per user with role-based access</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <ThunderboltOutlined style={{ color: '#ff9f0a' }} />
              </span>
              <div className="login-feature-title">SQL Gateway</div>
              <div className="login-feature-desc">Execute queries with guardrails and audit trails</div>
            </div>
          </div>

          <div className="login-brand-version">v0.1.0 - Apache 2.0 License</div>
        </div>
      </div>

      {/* Form Panel */}
      <div className="login-form-panel">
        <div className="login-form-header">
          <h2 className="login-form-title">Welcome back</h2>
          <p className="login-form-desc">Sign in to manage your PolarDB instances</p>
        </div>

        <Form onFinish={handleSubmit} layout="vertical" size="large" requiredMark={false}>
          <Form.Item name="username" rules={[{ required: true, message: 'Please enter your username' }]}>
            <Input
              prefix={<UserOutlined />}
              placeholder="Username"
              autoFocus
            />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: 'Please enter your password' }]}>
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="Password"
            />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              className="login-submit-btn"
            >
              Sign In
            </Button>
          </Form.Item>
        </Form>

        <div className="login-footer">
          alibabacloud polardb tool agentic server - Open Source MCP Gateway
        </div>
      </div>
    </div>
  )
}
