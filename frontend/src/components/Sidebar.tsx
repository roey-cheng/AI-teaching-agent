import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

import { useLanguage } from '../i18n'
import type { ChatSession, User } from '../types'
import { Icon } from './Icons'
import { LanguageToggle } from './LanguageToggle'

type Props = {
  user: User
  sessions: ChatSession[]
  activeSessionId: string | null
  open: boolean
  disabled: boolean
  onClose: () => void
  onCreate: () => void
  onSelect: (sessionId: string) => void
  onRename: (sessionId: string, title: string) => Promise<void>
  onMemory: () => void
  onLogout: () => void
}

export function Sidebar(props: Props) {
  const { t } = useLanguage()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const editRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editingId) editRef.current?.focus()
  }, [editingId])

  function beginRename(session: ChatSession) {
    setEditingId(session.session_id)
    setTitle(session.title)
  }

  async function rename(event: FormEvent) {
    event.preventDefault()
    if (!editingId || !title.trim()) return
    await props.onRename(editingId, title.trim())
    setEditingId(null)
  }

  return (
    <>
      <div className={`sidebar-backdrop ${props.open ? 'visible' : ''}`} onClick={props.onClose} />
      <aside className={`sidebar ${props.open ? 'open' : ''}`} aria-label={t('recentChats')}>
        <div className="sidebar-top">
          <div className="brand"><span className="brand-mark"><Icon name="sparkle" /></span><span>Mentor</span></div>
          <button aria-label={t('closeSidebar')} className="icon-button mobile-only" onClick={props.onClose} type="button"><Icon name="close" /></button>
        </div>

        <button className="new-chat-button" disabled={props.disabled} onClick={props.onCreate} type="button">
          <Icon name="plus" /><span>{t('newChat')}</span>
        </button>

        <nav className="session-nav" aria-label={t('recentChats')}>
          <p className="nav-label">{t('recentChats')}</p>
          {props.sessions.length === 0 ? (
            <p className="sidebar-empty">{t('noChats')}</p>
          ) : (
            <ul className="session-list">
              {props.sessions.map((session) => (
                <li className={session.session_id === props.activeSessionId ? 'active' : ''} key={session.session_id}>
                  {editingId === session.session_id ? (
                    <form className="rename-form" onSubmit={rename}>
                      <input
                        maxLength={100}
                        onBlur={() => setEditingId(null)}
                        onChange={(event) => setTitle(event.target.value)}
                        onKeyDown={(event) => { if (event.key === 'Escape') setEditingId(null) }}
                        ref={editRef}
                        value={title}
                      />
                    </form>
                  ) : (
                    <>
                      <button
                        className="session-select"
                        disabled={props.disabled && session.session_id !== props.activeSessionId}
                        onClick={() => props.onSelect(session.session_id)}
                        title={session.title}
                        type="button"
                      >
                        <Icon name="chat" /><span>{session.title}</span>
                      </button>
                      <button aria-label={`${t('rename')} ${session.title}`} className="rename-button" disabled={props.disabled} onClick={() => beginRename(session)} type="button"><Icon name="edit" /></button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-language"><LanguageToggle compact /></div>
          <button className="sidebar-action" onClick={props.onMemory} type="button"><Icon name="memory" /><span>{t('profileMemory')}</span><Icon className="action-chevron" name="chevron" /></button>
          <div className="account-block">
            <span className="avatar">{props.user.display_name.slice(0, 1).toUpperCase()}</span>
            <span className="account-text"><strong>{props.user.display_name}</strong><small>{props.user.email}</small></span>
            <button aria-label={t('logout')} className="icon-button" onClick={props.onLogout} title={t('logout')} type="button"><Icon name="logout" /></button>
          </div>
        </div>
      </aside>
    </>
  )
}
