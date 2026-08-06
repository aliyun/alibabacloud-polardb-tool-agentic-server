import { useState } from 'react'
import { Form, Input, Button, message } from 'antd'
import { UserOutlined, LockOutlined, ApiOutlined, CloudOutlined, TeamOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import LanguageSwitcher from '../../components/LanguageSwitcher'
import './Login.css'

interface LoginProps {
  onLogin: (username: string, password: string) => Promise<void>
}

export default function Login({ onLogin }: LoginProps) {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      await onLogin(values.username, values.password)
      navigate('/dashboard')
    } catch {
      message.error(t('auth.invalidCredentials'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-language"><LanguageSwitcher /></div>
      {/* Brand Panel */}
      <div className="login-brand">
        <div className="login-brand-content">
          <h1 className="login-brand-title">alibabacloud polardb tool agentic server</h1>
          <p className="login-brand-subtitle">
            {t('auth.brandLineOne')}<br />
            {t('auth.brandLineTwo')}
          </p>

          <div className="login-features">
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <ApiOutlined style={{ color: '#5ac8fa' }} />
              </span>
              <div className="login-feature-title">{t('auth.protocolTitle')}</div>
              <div className="login-feature-desc">{t('auth.protocolDescription')}</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <CloudOutlined style={{ color: '#34c759' }} />
              </span>
              <div className="login-feature-title">{t('auth.provisioningTitle')}</div>
              <div className="login-feature-desc">{t('auth.provisioningDescription')}</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <TeamOutlined style={{ color: '#af52de' }} />
              </span>
              <div className="login-feature-title">{t('auth.multitenantTitle')}</div>
              <div className="login-feature-desc">{t('auth.multitenantDescription')}</div>
            </div>
            <div className="login-feature-card">
              <span className="login-feature-icon">
                <ThunderboltOutlined style={{ color: '#ff9f0a' }} />
              </span>
              <div className="login-feature-title">{t('auth.sqlGatewayTitle')}</div>
              <div className="login-feature-desc">{t('auth.sqlGatewayDescription')}</div>
            </div>
          </div>

          <div className="login-brand-version">v0.1.0 - Apache 2.0 License</div>
        </div>
      </div>

      {/* Form Panel */}
      <div className="login-form-panel">
        <div className="login-form-header">
          <h2 className="login-form-title">{t('auth.welcomeTitle')}</h2>
          <p className="login-form-desc">{t('auth.welcomeDescription')}</p>
        </div>

        <Form onFinish={handleSubmit} layout="vertical" size="large" requiredMark={false}>
          <Form.Item name="username" rules={[{ required: true, message: t('auth.usernameRequired') }]}>
            <Input
              prefix={<UserOutlined />}
              placeholder={t('auth.username')}
              autoFocus
            />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: t('auth.passwordRequired') }]}>
            <Input.Password
              prefix={<LockOutlined />}
              placeholder={t('auth.password')}
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
              {t('auth.signIn')}
            </Button>
          </Form.Item>
        </Form>

        <div className="login-footer">
          alibabacloud polardb tool agentic server - {t('auth.footer')}
        </div>
      </div>
    </div>
  )
}
