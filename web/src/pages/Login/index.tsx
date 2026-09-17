import { useState } from 'react'
import { Form, Input, Button, message } from 'antd'
import {
  ApiOutlined,
  CloudOutlined,
  LockOutlined,
  LoginOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import api from '../../api/client'
import LanguageSwitcher from '../../components/LanguageSwitcher'
import type { AuthModeInfo } from '../../hooks/useAuth'
import './Login.css'

interface LoginProps {
  onLogin: (username: string, password: string) => Promise<void>
  authModeInfo?: AuthModeInfo
  recovery?: boolean
}

export default function Login({
  onLogin,
  authModeInfo,
  recovery = false,
}: LoginProps) {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      if (recovery) {
        await api.post('/auth/recovery/login', values, {
          pasSkipAuthRedirect: true,
        })
        window.location.assign('/dashboard')
        return
      } else {
        await onLogin(values.username, values.password)
      }
      navigate('/dashboard')
    } catch {
      message.error(t('auth.invalidCredentials'))
    } finally {
      setLoading(false)
    }
  }
  const oidcMode = authModeInfo?.mode === 'oidc' && !recovery
  const providerName = authModeInfo?.provider_name || t('auth.enterpriseSSO')

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
          <h2 className="login-form-title">
            {recovery ? t('auth.recoveryTitle') : t('auth.welcomeTitle')}
          </h2>
          <p className="login-form-desc">
            {recovery
              ? t('auth.recoveryDescription')
              : oidcMode
                ? t('auth.ssoDescription', { provider: providerName })
                : t('auth.welcomeDescription')}
          </p>
        </div>

        {oidcMode ? (
          <Button
            type="primary"
            size="large"
            block
            className="login-submit-btn"
            icon={<LoginOutlined />}
            href={authModeInfo?.sso_login_url || '/auth/oidc/login'}
            aria-label={t('auth.signInWithSSO', { provider: providerName })}
          >
            {t('auth.signInWithSSO', { provider: providerName })}
          </Button>
        ) : (
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
            {!recovery && (
              <>
                <Form.Item>
                  <Button
                    block
                    onClick={() => window.location.assign('/auth/feishu/login')}
                  >
                    {t('auth.signInWithFeishu')}
                  </Button>
                </Form.Item>
                <Form.Item>
                  <Button
                    block
                    onClick={() => window.location.assign('/auth/sharepoint/login')}
                  >
                    {t('auth.signInWithSharePoint')}
                  </Button>
                </Form.Item>
              </>
            )}
          </Form>
        )}

        {oidcMode && authModeInfo?.recovery_login_path && (
          <Button
            type="link"
            block
            href={authModeInfo.recovery_login_path}
            className="login-recovery-link"
          >
            {t('auth.recoveryLink')}
          </Button>
        )}

        <div className="login-footer">
          alibabacloud polardb tool agentic server - {t('auth.footer')}
        </div>
      </div>
    </div>
  )
}
