// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { sessionApi } from '../api/client'
import { LanguageProvider } from '../i18n'
import type { LiveGeneration, MessageHistory } from '../types'
import { ChatPanel } from './ChatPanel'

vi.mock('../api/client', () => ({
  sessionApi: { reasoning: vi.fn() },
  friendlyErrorMessage: (_code: string, message: string) => message,
}))
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('hands live reasoning over to a collapsed persisted card and reloads after remount', async () => {
  window.localStorage.setItem('mentor-language', 'zh')
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(sessionApi.reasoning).mockResolvedValue({
    message_id: '2', available: true, steps: [{ step: 1, text: 'saved reasoning' }],
  })
  const live: LiveGeneration = {
    sessionId: '10', mode: 'send', userContent: 'question', attemptId: 'attempt',
    stage: 'answering', reasoning: 'live reasoning', answer: 'answer', error: null,
  }
  const history: MessageHistory = {
    session_id: '10', is_generating: false, items: [{
      message_id: '2', role: 'ASSISTANT', content: 'answer',
      in_reply_to_message_id: '1', created_at: '2026-10-17T00:00:00Z',
    }],
  }
  function panel(current: LiveGeneration | null, saved: MessageHistory | null) {
    return <LanguageProvider><ChatPanel
      session={null} history={saved} historyLoading={false} live={current}
      onMenu={vi.fn()} onSend={vi.fn()} onRetry={vi.fn()}
    /></LanguageProvider>
  }
  const view = render(panel(live, null))
  expect(screen.getByText('live reasoning')).toBeTruthy()
  view.rerender(panel(null, history))
  expect(screen.queryByText('live reasoning')).toBeNull()
  expect(screen.getByRole('button', { name: '查看模型思考' }).getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(screen.getByRole('button', { name: '查看模型思考' }))
  expect(await screen.findByText('saved reasoning')).toBeTruthy()
  view.unmount()
  render(panel(null, history))
  expect(screen.queryByText('saved reasoning')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看模型思考' }))
  expect(await screen.findByText('saved reasoning')).toBeTruthy()
  expect(sessionApi.reasoning).toHaveBeenCalledTimes(2)
})
