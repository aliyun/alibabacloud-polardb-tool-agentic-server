import React from 'react'
import ReactDOM from 'react-dom/client'
import './styles/global.css'
import './styles/animations.css'
import App from './App'
import LocaleProvider from './i18n/LocaleProvider'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <LocaleProvider>
      <App />
    </LocaleProvider>
  </React.StrictMode>,
)
