import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

import { friendlyErrorMessage } from '../api/client'
import { useLanguage } from '../i18n'
import type { MessageKey } from '../i18n'
import type { AgentStage, ChatSession, LiveGeneration, MessageHistory, UserMessage } from '../types'
import { Icon } from './Icons'
import { Markdown } from './Markdown'

const stageLabelKeys: Record<AgentStage, MessageKey> = {
  context_ready: 'contextReady',
  agent_running: 'agentRunning',
  thinking: 'thinking',
  answering: 'answering',
  saving: 'saving',
  memory_checking: 'memoryChecking',
  memory_saving: 'memorySaving',
  memory_saved: 'memorySaved',
  memory_skipped: 'memorySkipped',
  memory_unavailable: 'memoryUnavailable',
}

type Props = {
  session: ChatSession | null
  history: MessageHistory | null
  historyLoading: boolean
  live: LiveGeneration | null
  onMenu: () => void
  onSend: (content: string) => Promise<void>
  onRetry: (message: UserMessage) => Promise<void>
}

export function ChatPanel({ session, history, historyLoading, live, onMenu, onSend, onRetry }: Props) {
  const { language, t } = useLanguage()
  const [content, setContent] = useState('')
  const [thinkingOpen, setThinkingOpen] = useState(true)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const busy = Boolean(live) || Boolean(history?.is_generating)
  const hasMessages = Boolean(history?.items.length)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: live ? 'smooth' : 'auto' })
  }, [history?.items.length, live?.answer, live?.reasoning, live?.stage])

  useEffect(() => {
    setThinkingOpen(true)
  }, [live?.attemptId])

  function resizeTextarea() {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = '0px'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`
  }

  async function submit() {
    const value = content
    if (!value.trim() || busy) return
    setContent('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    await onSend(value)
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void submit()
    }
  }

  const showWelcome = !historyLoading && !hasMessages && !live
  const suggestions = [
    [t('explainConcept'), t('explainPrompt')],
    [t('learningPlan'), t('planPrompt')],
    [t('readCode'), t('codePrompt')],
  ]

  return (
    <main className="chat-main">
      <header className="chat-header">
        <button aria-label={t('openSidebar')} className="icon-button menu-button" onClick={onMenu} type="button"><Icon name="menu" /></button>
        <div className="chat-title"><strong>{session?.title ?? t('newConversation')}</strong><span>{t('agentLabel')}</span></div>
        <div className="model-badge"><span />{t('deepAgent')}</div>
      </header>

      <section className={`conversation ${showWelcome ? 'welcome-layout' : ''}`} aria-live="polite">
        {historyLoading && <div className="loading-history"><span className="spinner dark" />{t('loadingHistory')}</div>}

        {showWelcome && (
          <div className="welcome">
            <span className="welcome-mark"><Icon name="sparkle" /></span>
            <h1>{t('welcomeQuestion')}</h1>
            <p>{t('welcomeHint')}</p>
            <div className="suggestion-grid">
              {suggestions.map(([title, prompt]) => (
                <button key={title} onClick={() => void onSend(prompt)} type="button">
                  <strong>{title}</strong><span>{prompt}</span><Icon name="chevron" />
                </button>
              ))}
            </div>
          </div>
        )}

        {!historyLoading && history?.items.map((message) => message.role === 'USER' ? (
          <div className="message-row user-row" key={message.message_id}>
            <div className="user-message">{message.content}</div>
            {message.generation.status === 'FAILED' && (
              <div className="generation-failure">
                <span>{message.generation.error
                  ? friendlyErrorMessage(message.generation.error.code, message.generation.error.message, language)
                  : t('genericFailure')}</span>
                {message.generation.can_retry && (
                  <button disabled={busy} onClick={() => void onRetry(message)} type="button"><Icon name="retry" />{t('retryAnswer')}</button>
                )}
              </div>
            )}
            {message.generation.status === 'RUNNING' && !live && <div className="generation-running"><span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />{t('stillRunning')}</div>}
          </div>
        ) : (
          <div className="message-row assistant-row" key={message.message_id}>
            <span className="assistant-avatar"><Icon name="sparkle" /></span>
            <div className="assistant-message"><Markdown>{message.content}</Markdown></div>
          </div>
        ))}

        {live?.mode === 'send' && <div className="message-row user-row"><div className="user-message">{live.userContent}</div></div>}

        {live && (
          <div className="message-row assistant-row live-row">
            <span className="assistant-avatar"><Icon name="sparkle" /></span>
            <div className="live-content">
              <div className="agent-status"><span className="pulse-dot" />{live.stage ? t(stageLabelKeys[live.stage]) : t('receiving')}</div>
              {live.reasoning && (
                <div className="thinking-card">
                  <button aria-expanded={thinkingOpen} onClick={() => setThinkingOpen((open) => !open)} type="button">
                    <span><Icon name="brain" />{t('modelReasoning')}</span><Icon className={thinkingOpen ? 'rotate' : ''} name="chevron" />
                  </button>
                  {thinkingOpen && <div className="thinking-text">{live.reasoning}</div>}
                </div>
              )}
              {live.answer ? <div className="assistant-message"><Markdown>{live.answer}</Markdown><span className="stream-caret" /></div> : !live.error && <div className="answer-placeholder"><i /><i /><i /></div>}
              {live.error && <div className="stream-error">{friendlyErrorMessage(live.error.code, live.error.message, language)}</div>}
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </section>

      <footer className="composer-area">
        <div className={`composer ${busy ? 'busy' : ''}`}>
          <textarea
            aria-label={t('sendPlaceholder')}
            disabled={busy}
            maxLength={20000}
            onChange={(event) => { setContent(event.target.value); resizeTextarea() }}
            onKeyDown={keyDown}
            placeholder={busy ? t('generating') : t('sendPlaceholder')}
            ref={textareaRef}
            rows={1}
            value={content}
          />
          <button aria-label={t('sendMessage')} className="send-button" disabled={busy || !content.trim()} onClick={() => void submit()} type="button"><Icon name="send" /></button>
        </div>
        <p>{t('composerHint')}</p>
      </footer>
    </main>
  )
}
