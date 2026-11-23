// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import App from './App'
import { accountApi, sessionApi, streamChat } from './api/client'
import { LanguageProvider } from './i18n'
import type { MessageHistory, StreamEvent } from './types'

vi.mock('./api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('./api/client')>()
  return {
    ...original,
    accountApi: { ...original.accountApi, me: vi.fn() },
    sessionApi: { ...original.sessionApi, list: vi.fn(), history: vi.fn() },
    streamChat: vi.fn(),
  }
})

afterEach(() => { cleanup(); vi.resetAllMocks() })

it('shows saved before syncing history and removes the live state after history loads', async () => {
  window.localStorage.setItem('mentor-language', 'zh')
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(accountApi.me).mockResolvedValue({ user_id: '1', display_name: 'Test', email: 'test@example.invalid' })
  vi.mocked(sessionApi.list).mockResolvedValue([{
    session_id: '10', title: 'Test session', created_at: '', updated_at: '', last_activity_at: '',
  }])
  let finishHistory!: (value: MessageHistory) => void
  const historyRefresh = new Promise<MessageHistory>((resolve) => { finishHistory = resolve })
  vi.mocked(sessionApi.history)
    .mockResolvedValueOnce({ session_id: '10', is_generating: false, items: [] })
    .mockReturnValueOnce(historyRefresh)
  let emit!: (event: StreamEvent) => void
  let finishStream!: () => void
  vi.mocked(streamChat).mockImplementation((_path, _body, onEvent) => {
    emit = onEvent
    return new Promise((resolve) => { finishStream = () => resolve(null) })
  })
  render(<LanguageProvider><App /></LanguageProvider>)
  await screen.findByText('今天想学点什么？')
  await waitFor(() => expect(sessionApi.history).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByRole('textbox', { name: '给 Mentor 发消息' }), { target: { value: 'question' } })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
  await act(async () => {
    emit({ type: 'message_delta', data: { attempt_id: 'attempt', text: 'answer' } })
    emit({ type: 'agent_progress', data: { attempt_id: 'attempt', stage: 'saving' } })
  })
  expect(screen.getByText('正在保存对话')).toBeTruthy()
  const assistantMessage = {
    message_id: '2', role: 'ASSISTANT' as const, content: 'answer',
    in_reply_to_message_id: '1', created_at: '2026-10-17T00:00:00Z',
  }
  await act(async () => emit({ type: 'message_done', data: { attempt_id: 'attempt', assistant_message: assistantMessage } }))
  expect(screen.getByText('对话已保存')).toBeTruthy()
  expect(screen.queryByText('正在保存对话')).toBeNull()
  await act(async () => finishStream())
  expect(screen.getByText('对话已保存，正在同步历史记录')).toBeTruthy()
  expect((screen.getByRole('textbox', { name: '给 Mentor 发消息' }) as HTMLTextAreaElement).disabled).toBe(true)
  await act(async () => finishHistory({ session_id: '10', is_generating: false, items: [assistantMessage] }))
  expect(screen.queryByText('对话已保存，正在同步历史记录')).toBeNull()
  expect(screen.getAllByText('answer')).toHaveLength(1)
  expect((screen.getByRole('textbox', { name: '给 Mentor 发消息' }) as HTMLTextAreaElement).disabled).toBe(false)
})
