import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { LanguageProvider } from './i18n'
import './styles.css'

const root = document.getElementById('root')
if (!root) throw new Error('找不到页面入口 #root')

createRoot(root).render(
  <StrictMode>
    <LanguageProvider><App /></LanguageProvider>
  </StrictMode>,
)
