import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import api from '../api/client'

// Isolated embedded pages retain their previous defaults. The console always
// installs this provider, which starts closed and reads runtime admission.
const FeaturesContext = createContext({ knowledge: true, loaded: true })

export function FeaturesProvider({ children }: { children: ReactNode }) {
  const [value, setValue] = useState({ knowledge: false, loaded: false })
  useEffect(() => {
    let mounted = true
    const refresh = async () => {
      try {
        const response = await api.get('/api/features')
        if (mounted) setValue({ knowledge: response.data.knowledge.available === true, loaded: true })
      } catch {
        if (mounted) setValue({ knowledge: false, loaded: true })
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [])
  return <FeaturesContext.Provider value={value}>{children}</FeaturesContext.Provider>
}

export function useFeatures() { return useContext(FeaturesContext) }
