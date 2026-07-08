import { Spin } from 'antd'
import { Navigate } from 'react-router-dom'
import type { UserInfo } from '../../hooks/useAuth'

interface AuthGuardProps {
  user: UserInfo | null
  loading: boolean
  children: React.ReactNode
}

export default function AuthGuard({ user, loading, children }: AuthGuardProps) {
  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>
        <Spin size="large" />
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  return <>{children}</>
}
