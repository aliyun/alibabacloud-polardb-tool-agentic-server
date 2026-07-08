import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Spin } from 'antd'
import { useAuth } from './hooks/useAuth'
import AppLayout from './components/Layout'
import AuthGuard from './components/AuthGuard'
import Login from './pages/Login'

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

function PageLoading() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '40vh' }}>
      <Spin size="large" />
    </div>
  )
}

function App() {
  const { user, loading, login, logout, authMode } = useAuth()

  return (
    <BrowserRouter>
      <Suspense fallback={<PageLoading />}>
        <Routes>
          <Route path="/login" element={<Login onLogin={login} />} />
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
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  )
}

export default App
