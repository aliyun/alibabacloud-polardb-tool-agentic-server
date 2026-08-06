import { lazy, Suspense, useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Button, Skeleton } from 'antd'
import { useTranslation } from 'react-i18next'
import { useAuth } from './hooks/useAuth'
import { discoverSystemState } from './api/configuration'
import AppLayout from './components/Layout'
import AuthGuard from './components/AuthGuard'
import Login from './pages/Login'
import Setup from './pages/Setup'

// Lazy-loaded pages for code splitting
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Users = lazy(() => import('./pages/Users'))
const Departments = lazy(() => import('./pages/Departments'))
const Instances = lazy(() => import('./pages/Instances'))
const InstanceDetail = lazy(() => import('./pages/InstanceDetail'))
const MyInstances = lazy(() => import('./pages/MyInstances'))
const AuditLogs = lazy(() => import('./pages/AuditLogs'))
const Settings = lazy(() => import('./pages/Settings'))
const Pool = lazy(() => import('./pages/Pool'))
const Agents = lazy(() => import('./pages/Agents'))
const AgentDetail = lazy(() => import('./pages/AgentDetail'))

function PageLoading() {
  return (
    <div style={{ maxWidth: 720, margin: '12vh auto', padding: 32 }}>
      <Skeleton active paragraph={{ rows: 7 }} />
    </div>
  )
}

interface ReadyRoutesProps {
  setupDestination?: '/dashboard' | '/settings/configuration'
}

function ReadyRoutes({
  setupDestination = '/settings/configuration',
}: ReadyRoutesProps) {
  const { user, loading, login, logout, authMode } = useAuth()

  return (
    <Routes>
      <Route path="/login" element={<Login onLogin={login} />} />
      <Route
        path="/setup"
        element={<Navigate to={setupDestination} replace />}
      />
      <Route
        element={
          <AuthGuard user={user} loading={loading}>
            <AppLayout user={user!} onLogout={logout} authMode={authMode} />
          </AuthGuard>
        }
      >
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/users" element={<Users />} />
        <Route path="/departments" element={<Departments />} />
        <Route path="/instances" element={<Instances />} />
        <Route path="/instances/:id" element={<InstanceDetail />} />
        <Route path="/my-instances" element={<MyInstances />} />
        <Route path="/audit-logs" element={<AuditLogs />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/pool" element={<Pool />} />
        {(loading || user?.role === 'admin') && (
          <>
            <Route path="/agents" element={<Agents />} />
            <Route path="/agents/:id" element={<AgentDetail />} />
            <Route
              path="/settings/configuration"
              element={<Setup mode="admin" />}
            />
          </>
        )}
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}

function App() {
  const { t } = useTranslation()
  const [systemState, setSystemState] = useState<'SETUP' | 'READY' | 'ERROR'>()
  const [discoveryAttempt, setDiscoveryAttempt] = useState(0)
  const [completedSetup, setCompletedSetup] = useState(false)

  useEffect(() => {
    discoverSystemState()
      .then(setSystemState)
      .catch(() => setSystemState('ERROR'))
  }, [discoveryAttempt])

  return (
    <BrowserRouter>
      <Suspense fallback={<PageLoading />}>
        {!systemState ? (
          <PageLoading />
        ) : systemState === 'ERROR' ? (
          <main
            role="alert"
            style={{ maxWidth: 640, margin: '14vh auto', padding: 32, textAlign: 'center' }}
          >
            <h1>{t('app.serverStateTitle')}</h1>
            <p>{t('app.serverStateDescription')}</p>
            <Button
              type="primary"
              onClick={() => {
                setSystemState(undefined)
                setDiscoveryAttempt((attempt) => attempt + 1)
              }}
            >
              {t('common.retry')}
            </Button>
          </main>
        ) : systemState === 'SETUP' ? (
          <Routes>
            <Route
              path="/setup"
              element={(
                <Setup
                  onEnterConsole={() => {
                    setCompletedSetup(true)
                    setSystemState('READY')
                  }}
                />
              )}
            />
            <Route path="*" element={<Navigate to="/setup" replace />} />
          </Routes>
        ) : (
          <ReadyRoutes
            setupDestination={completedSetup ? '/dashboard' : undefined}
          />
        )}
      </Suspense>
    </BrowserRouter>
  )
}

export default App
