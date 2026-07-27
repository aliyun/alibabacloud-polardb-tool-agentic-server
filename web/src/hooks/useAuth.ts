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

export function useAuth() {
  const [user, setUser] = useState<UserInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [authMode, setAuthMode] = useState<string>('builtin')

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
    api.get('/auth/mode').then(r => setAuthMode(r.data.mode)).catch(() => {})
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

  return { user, loading, login, logout, isAdmin: user?.role === 'admin', authMode }
}
