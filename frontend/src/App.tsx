import { useCallback, useEffect, useMemo, useState } from 'react'

import { accountApi, ApiError, sessionApi, streamChat, userFacingError } from './api/client'
import { AuthScreen } from './components/AuthScreen'
import { ChatPanel } from './components/ChatPanel'
import { Icon } from './components/Icons'
import { MemoryDrawer } from './components/MemoryDrawer'
import { Sidebar } from './components/Sidebar'
import { useLanguage } from './i18n'
import type { ChatSession, LiveGeneration, MessageHistory, StreamEvent, User, UserMessage } from './types'

type AuthState =
  | { kind: 'checking' }
  | { kind: 'guest' }
  | { kind: 'authenticated'; user: User }
  | { kind: 'unavailable'; error: unknown }

export default function App() {
  const { language, t } = useLanguage()
  const [auth, setAuth] = useState<AuthState>({ kind: 'checking' })
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [history, setHistory] = useState<MessageHistory | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [live, setLive] = useState<LiveGeneration | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [toast, setToast] = useState('')

  const user = auth.kind === 'authenticated' ? auth.user : null
  const activeSession = useMemo(
    () => sessions.find((session) => session.session_id === activeSessionId) ?? null,
    [activeSessionId, sessions],
  )

  const checkAuthentication = useCallback(async () => {
    setAuth({ kind: 'checking' })
    try {
      setAuth({ kind: 'authenticated', user: await accountApi.me() })
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setAuth({ kind: 'guest' })
      else setAuth({ kind: 'unavailable', error })
    }
  }, [])

  useEffect(() => { void checkAuthentication() }, [checkAuthentication])

  useEffect(() => {
    function authenticationExpired() {
      setAuth({ kind: 'guest' })
      setSessions([])
      setActiveSessionId(null)
      setHistory(null)
      setLive(null)
    }
    window.addEventListener('mentor:authentication-expired', authenticationExpired)
    return () => window.removeEventListener('mentor:authentication-expired', authenticationExpired)
  }, [])

  const refreshSessions = useCallback(async (preferredId?: string) => {
    const nextSessions = await sessionApi.list()
    setSessions(nextSessions)
    setActiveSessionId((current) => {
      if (preferredId && nextSessions.some((item) => item.session_id === preferredId)) return preferredId
      if (current && nextSessions.some((item) => item.session_id === current)) return current
      return nextSessions[0]?.session_id ?? null
    })
    return nextSessions
  }, [])

  useEffect(() => {
    if (!user) return
    refreshSessions().catch((error) => setToast(userFacingError(error, language)))
  }, [language, refreshSessions, user])

  const refreshHistory = useCallback(async (sessionId: string) => {
    const nextHistory = await sessionApi.history(sessionId)
    setHistory((current) => activeSessionId === sessionId || current?.session_id === sessionId ? nextHistory : current)
    return nextHistory
  }, [activeSessionId])

  useEffect(() => {
    if (!activeSessionId) {
      setHistory(null)
      return
    }
    let active = true
    setHistoryLoading(true)
    setHistory(null)
    sessionApi.history(activeSessionId)
      .then((result) => { if (active) setHistory(result) })
      .catch((error) => { if (active) setToast(userFacingError(error, language)) })
      .finally(() => { if (active) setHistoryLoading(false) })
    return () => { active = false }
  }, [activeSessionId, language])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 5000)
    return () => window.clearTimeout(timer)
  }, [toast])

  async function createSession(): Promise<ChatSession | null> {
    if (live) return null
    try {
      const created = await sessionApi.create()
      setSessions((current) => [created, ...current])
      setActiveSessionId(created.session_id)
      setHistory({ session_id: created.session_id, is_generating: false, items: [] })
      setSidebarOpen(false)
      return created
    } catch (error) {
      setToast(userFacingError(error, language))
      return null
    }
  }

  async function renameSession(sessionId: string, title: string) {
    try {
      const renamed = await sessionApi.rename(sessionId, title)
      setSessions((current) => current.map((session) => session.session_id === sessionId ? renamed : session))
    } catch (error) {
      setToast(userFacingError(error, language))
    }
  }

  function applyStreamEvent(event: StreamEvent) {
    setLive((current) => {
      if (!current) return current
      switch (event.type) {
        case 'message_start': return { ...current, attemptId: event.data.attempt_id }
        case 'agent_progress': return { ...current, attemptId: event.data.attempt_id, stage: event.data.stage }
        case 'reasoning_delta': return { ...current, attemptId: event.data.attempt_id, reasoning: current.reasoning + event.data.text }
        case 'message_delta': return { ...current, attemptId: event.data.attempt_id, answer: current.answer + event.data.text }
        case 'message_done': return { ...current, attemptId: event.data.attempt_id, stage: 'saving' }
        case 'message_error': return { ...current, attemptId: event.data.attempt_id, error: event.data.error }
      }
    })
  }

  async function runGeneration(sessionId: string, mode: 'send' | 'retry', content: string, path: string, body: unknown) {
    let terminalEvent = false
    setLive({ sessionId, mode, userContent: content, attemptId: null, stage: null, reasoning: '', answer: '', error: null })
    try {
      const duplicate = await streamChat(path, body, (event) => {
        if (event.type === 'message_done' || event.type === 'message_error') terminalEvent = true
        applyStreamEvent(event)
      })
      if (!duplicate && !terminalEvent) setToast(t('streamEnded'))
    } catch (error) {
      setToast(userFacingError(error, language))
    } finally {
      try {
        await Promise.all([refreshHistory(sessionId), refreshSessions(sessionId)])
      } catch (error) {
        setToast(userFacingError(error, language))
      }
      setLive(null)
    }
  }

  async function sendMessage(content: string) {
    const session = activeSession ?? await createSession()
    if (!session) return
    await runGeneration(session.session_id, 'send', content, `/api/v1/chat/sessions/${session.session_id}/messages`, {
      client_message_key: crypto.randomUUID(), content,
    })
  }

  async function retryMessage(message: UserMessage) {
    if (!activeSessionId) return
    await runGeneration(
      activeSessionId,
      'retry',
      message.content,
      `/api/v1/chat/sessions/${activeSessionId}/messages/${message.message_id}/retry`,
      { failed_attempt_id: message.generation.attempt_id },
    )
  }

  async function logout() {
    try {
      await accountApi.logout()
    } catch (error) {
      setToast(userFacingError(error, language))
    } finally {
      setAuth({ kind: 'guest' })
      setSessions([])
      setActiveSessionId(null)
      setHistory(null)
    }
  }

  if (auth.kind === 'checking') {
    return <main className="boot-screen"><span className="brand-mark"><Icon name="sparkle" /></span><div className="boot-line" /></main>
  }

  if (auth.kind === 'unavailable') {
    return <main className="error-screen"><span className="brand-mark"><Icon name="sparkle" /></span><h1>{t('backendUnavailable')}</h1><p>{userFacingError(auth.error, language)}</p><button className="primary-button" onClick={() => void checkAuthentication()} type="button">{t('reconnect')}</button></main>
  }

  if (auth.kind === 'guest') {
    return <AuthScreen onAuthenticated={(authenticatedUser) => setAuth({ kind: 'authenticated', user: authenticatedUser })} />
  }

  return (
    <div className="app-shell">
      <Sidebar
        activeSessionId={activeSessionId}
        disabled={Boolean(live)}
        onClose={() => setSidebarOpen(false)}
        onCreate={() => void createSession()}
        onLogout={() => void logout()}
        onMemory={() => { setMemoryOpen(true); setSidebarOpen(false) }}
        onRename={renameSession}
        onSelect={(sessionId) => { setActiveSessionId(sessionId); setSidebarOpen(false) }}
        open={sidebarOpen}
        sessions={sessions}
        user={auth.user}
      />
      <ChatPanel history={history} historyLoading={historyLoading} live={live} onMenu={() => setSidebarOpen(true)} onRetry={retryMessage} onSend={sendMessage} session={activeSession} />
      <MemoryDrawer onClose={() => setMemoryOpen(false)} open={memoryOpen} />
      {toast && <div className="toast" role="status"><span>{toast}</span><button aria-label={t('closeNotice')} onClick={() => setToast('')} type="button"><Icon name="close" /></button></div>}
    </div>
  )
}
