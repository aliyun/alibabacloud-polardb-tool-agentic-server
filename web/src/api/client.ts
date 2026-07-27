import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios'

declare module 'axios' {
  export interface AxiosRequestConfig {
    pasSkipAuthRedirect?: boolean
  }

  export interface InternalAxiosRequestConfig {
    pasSkipAuthRedirect?: boolean
  }
}

interface APIErrorBody {
  detail?: string | { message?: string }
}

const api = axios.create({
  baseURL: '',
  withCredentials: true,
  headers: {
    'X-PAS-CSRF': '1',
  },
})

const REFRESH_ENDPOINTS = ['/auth/refresh', '/auth/login']

let isRefreshing = false
let refreshPromise: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  try {
    const resp = await axios.post('/auth/refresh', null, { withCredentials: true })
    return resp.status === 200
  } catch {
    return false
  }
}

function redirectToLogin() {
  if (window.location.pathname !== '/login') {
    window.location.href = '/login'
  }
}

export function getAPIErrorMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError<APIErrorBody>(error)) return fallback
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  return detail?.message ?? fallback
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined
    const status = error.response?.status
    const url = originalRequest?.url ?? ''

    if (originalRequest?.pasSkipAuthRedirect) {
      return Promise.reject(error)
    }

    if (status !== 401 || !originalRequest || originalRequest._retried) {
      if (status === 401) {
        redirectToLogin()
      }
      return Promise.reject(error)
    }

    // Don't try to refresh the refresh/login endpoints themselves.
    if (REFRESH_ENDPOINTS.some((e) => url.includes(e))) {
      redirectToLogin()
      return Promise.reject(error)
    }

    if (!isRefreshing) {
      isRefreshing = true
      refreshPromise = refreshSession().finally(() => {
        isRefreshing = false
      })
    }

    try {
      const ok = await refreshPromise
      if (!ok) {
        redirectToLogin()
        return Promise.reject(error)
      }
    } catch {
      redirectToLogin()
      return Promise.reject(error)
    }

    originalRequest._retried = true
    return api(originalRequest)
  }
)

export default api
