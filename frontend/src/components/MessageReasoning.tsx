import { useEffect, useId, useRef, useState } from 'react'

import { sessionApi } from '../api/client'
import { useLanguage } from '../i18n'
import type { MessageReasoning as ReasoningData } from '../types'
import { Icon } from './Icons'

type Props = { sessionId: string; messageId: string }

// Key the child by both IDs so cached private data cannot survive a message/session change.
export function MessageReasoning(props: Props) {
  return <ReasoningCard key={`${props.sessionId}:${props.messageId}`} {...props} />
}

function ReasoningCard({ sessionId, messageId }: Props) {
  const { t } = useLanguage()
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<ReasoningData | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)
  const request = useRef<AbortController | null>(null)
  const panelId = useId()

  useEffect(() => () => request.current?.abort(), [])

  async function load() {
    if (request.current && !request.current.signal.aborted) return
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setFailed(false)
    try {
      const result = await sessionApi.reasoning(sessionId, messageId, controller.signal)
      if (!controller.signal.aborted) setData(result)
    } catch {
      if (!controller.signal.aborted) setFailed(true)
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false)
        request.current = null
      }
    }
  }

  function toggle() {
    setOpen(!open)
    if (!open && !data && !loading) void load()
  }

  return (
    <div className="thinking-card saved-reasoning">
      <button aria-expanded={open} aria-controls={panelId} onClick={toggle} type="button">
        <span><Icon name="brain" />{t('viewReasoning')}</span>
        <Icon className={open ? 'rotate' : ''} name="chevron" />
      </button>
      {open && <div id={panelId} className="thinking-text">
        {loading && <p role="status">{t('loadingReasoning')}</p>}
        {failed && <div role="alert">
          <p>{t('reasoningFailed')}</p>
          <button className="reasoning-retry" onClick={() => void load()} type="button">{t('retryReasoning')}</button>
        </div>}
        {data && !data.available && <p>{t('noReasoning')}</p>}
        {data?.available && <>
          <p className="reasoning-notice">{t('reasoningNotice')}</p>
          {data.steps.map((step) => <section key={step.step}>
            <h4>{t('reasoningStep')} {step.step}</h4>
            <div>{step.text}</div>
          </section>)}
        </>}
      </div>}
    </div>
  )
}
