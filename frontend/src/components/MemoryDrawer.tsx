import { useEffect, useState } from 'react'

import { memoryApi, userFacingError } from '../api/client'
import { useLanguage } from '../i18n'
import type { MessageKey } from '../i18n'
import type { Memory, MemoryType } from '../types'
import { Icon } from './Icons'

const labelKeys: Record<MemoryType, MessageKey> = {
  LEARNING_PREFERENCE: 'learningPreference',
  LEARNING_GOAL: 'learningGoal',
  PROGRAMMING_BACKGROUND: 'programmingBackground',
  PERSONAL_BACKGROUND: 'personalBackground',
  DAILY_PREFERENCE: 'dailyPreference',
}

export function MemoryDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { language, t } = useLanguage()
  const [items, setItems] = useState<Memory[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true)
    setError('')
    memoryApi.list()
      .then((memories) => { if (active) setItems(memories) })
      .catch((caught) => { if (active) setError(userFacingError(caught, language)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [language, open])

  return (
    <>
      <div className={`drawer-backdrop ${open ? 'visible' : ''}`} onClick={onClose} />
      <aside aria-hidden={!open} aria-label={t('profileMemory')} className={`memory-drawer ${open ? 'open' : ''}`}>
        <header className="drawer-header">
          <div><span className="drawer-icon"><Icon name="memory" /></span><h2>Profile Memory</h2></div>
          <button aria-label={t('closeMemory')} className="icon-button" onClick={onClose} type="button"><Icon name="close" /></button>
        </header>
        <p className="drawer-intro">{t('memoryIntro')}</p>

        <div className="memory-content">
          {loading && <div className="drawer-state"><span className="spinner dark" />{t('loadingMemory')}</div>}
          {error && <div className="drawer-error" role="alert">{error}</div>}
          {!loading && !error && items.length === 0 && (
            <div className="empty-memory">
              <span><Icon name="brain" /></span>
              <h3>{t('noMemory')}</h3>
              <p>{t('noMemoryHint')}</p>
            </div>
          )}
          {!loading && !error && items.map((memory) => (
            <article className="memory-card" key={memory.memory_id}>
              <span className={`memory-type type-${memory.memory_type.toLowerCase()}`}>{t(labelKeys[memory.memory_type])}</span>
              <p>{memory.summary}</p>
              <time dateTime={memory.updated_at}>{t('updatedOn')} {new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en', { month: 'short', day: 'numeric' }).format(new Date(memory.updated_at))}</time>
            </article>
          ))}
        </div>
        <footer className="drawer-footer"><Icon name="check" />{t('memoryPrivate')}</footer>
      </aside>
    </>
  )
}
