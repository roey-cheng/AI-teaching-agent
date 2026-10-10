import { useState } from 'react'
import type { FormEvent } from 'react'

import { accountApi, userFacingError } from '../api/client'
import { useLanguage } from '../i18n'
import type { User } from '../types'
import { Icon } from './Icons'
import { LanguageToggle } from './LanguageToggle'

type Mode = 'login' | 'register'

export function AuthScreen({ onAuthenticated }: { onAuthenticated: (user: User) => void }) {
  const { language, t } = useLanguage()
  const [mode, setMode] = useState<Mode>('login')
  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (mode === 'register') {
        await accountApi.register({ display_name: displayName, email, password })
      }
      const result = await accountApi.login({ email, password })
      onAuthenticated(result.user)
    } catch (caught) {
      setError(userFacingError(caught, language))
    } finally {
      setBusy(false)
    }
  }

  function switchMode(nextMode: Mode) {
    setMode(nextMode)
    setError('')
  }

  return (
    <main className="auth-page">
      <header className="auth-topbar">
        <div className="brand brand-large"><span className="brand-mark"><Icon name="sparkle" /></span><span>Mentor</span></div>
        <LanguageToggle />
      </header>
      <section className="auth-panel">
        <div className="auth-card">
          <p className="auth-tagline">{t('authTagline')}</p>
          <div className="auth-tabs" role="tablist" aria-label={t('accountActions')}>
            <button aria-selected={mode === 'login'} className={mode === 'login' ? 'active' : ''} onClick={() => switchMode('login')} role="tab" type="button">{t('login')}</button>
            <button aria-selected={mode === 'register'} className={mode === 'register' ? 'active' : ''} onClick={() => switchMode('register')} role="tab" type="button">{t('register')}</button>
          </div>
          <div className="auth-heading">
            <h2>{mode === 'login' ? t('welcomeBack') : t('createSpace')}</h2>
            <p>{mode === 'login' ? t('loginSubtitle') : t('registerSubtitle')}</p>
          </div>
          <form className="auth-form" onSubmit={submit}>
            {mode === 'register' && (
              <label>
                <span>{t('displayName')}</span>
                <input autoComplete="name" maxLength={100} onChange={(e) => setDisplayName(e.target.value)} placeholder={t('namePlaceholder')} required value={displayName} />
              </label>
            )}
            <label>
              <span>{t('email')}</span>
              <input autoComplete="email" onChange={(e) => setEmail(e.target.value)} placeholder={t('emailPlaceholder')} required type="email" value={email} />
            </label>
            <label>
              <span>{t('password')}</span>
              <input autoComplete={mode === 'login' ? 'current-password' : 'new-password'} minLength={mode === 'register' ? 8 : 1} maxLength={128} onChange={(e) => setPassword(e.target.value)} placeholder={mode === 'register' ? t('newPasswordPlaceholder') : t('passwordPlaceholder')} required type="password" value={password} />
            </label>
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary-button auth-submit" disabled={busy} type="submit">
              {busy ? <span className="spinner" /> : null}
              {busy ? t('pleaseWait') : mode === 'login' ? t('login') : t('registerAndEnter')}
            </button>
          </form>
          <p className="auth-switch">
            {mode === 'login' ? t('noAccount') : t('hasAccount')}{' '}
            <button onClick={() => switchMode(mode === 'login' ? 'register' : 'login')} type="button">
              {mode === 'login' ? t('registerNow') : t('goLogin')}
            </button>
          </p>
        </div>
      </section>
    </main>
  )
}
