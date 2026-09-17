import { useState, useEffect, useCallback } from 'react'
import api from '../api/client'

export interface UserInfo {
  id: string
  external_id: string
  display_name: string
  email: string | null
  role: string
  status: string
}

export interface AuthModeInfo {
  mode: 'builtin' | 'oidc'
  provider_name: string | null
  sso_login_url: string | null
  recovery_login_path: string | null
}

export function useAuth() {
  const [user, setUser] = useState<UserInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [authModeInfo, setAuthModeInfo] = useState<AuthModeInfo>({
    mode: 'builtin',
    provider_name: null,
    sso_login_url: null,
    recovery_login_path: null,
  })

  const fetchUser = useCallback(async () => {
    try {
      const resp = await api.get('/auth/me')
      setUser(resp.data)
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchUser()
    api.get('/auth/mode').then(r => setAuthModeInfo(r.data)).catch(() => {})
  }, [fetchUser])

  const login = async (username: string, password: string) => {
    const resp = await api.post('/auth/login', { username, password })
    await fetchUser()
    return resp.data
  }

  const logout = async () => {
    await api.post('/auth/logout')
    setUser(null)
  }

  return {
    user,
    loading,
    login,
    logout,
    isAdmin: user?.role === 'admin',
    authMode: authModeInfo.mode,
    authModeInfo,
  }
}
