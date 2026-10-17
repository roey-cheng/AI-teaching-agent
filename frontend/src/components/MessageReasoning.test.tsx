// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionApi } from '../api/client'
import { LanguageProvider } from '../i18n'
import type { MessageReasoning as ReasoningData } from '../types'
import { MessageReasoning } from './MessageReasoning'

vi.mock('../api/client', () => ({ sessionApi: { reasoning: vi.fn() } }))
const query = vi.mocked(sessionApi.reasoning)
const result: ReasoningData = {
  message_id: '2', available: true,
  steps: [{ step: 1, text: 'first thought' }, { step: 2, text: '<script>alert(1)</script>\nsecond' }],
}
function card(sessionId = '10', messageId = '2') {
  return <LanguageProvider><MessageReasoning sessionId={sessionId} messageId={messageId} /></LanguageProvider>
}
function toggle() { fireEvent.click(screen.getByRole('button', { name: '查看模型思考' })) }

beforeEach(() => {
  window.localStorage.setItem('mentor-language', 'zh')
  query.mockReset()
  query.mockResolvedValue(result)
})
afterEach(cleanup)

describe('saved model reasoning', () => {
  it('starts collapsed, fetches on demand and safely renders separate plain-text steps', async () => {
    const { container } = render(card())
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false')
    expect(query).not.toHaveBeenCalled()
    toggle()
    expect(await screen.findByText('first thought')).toBeTruthy()
    expect(screen.getByText('思考步骤 2')).toBeTruthy()
    expect(container.textContent).toContain('<script>alert(1)</script>')
    expect(container.querySelector('script')).toBeNull()
    expect(query).toHaveBeenCalledWith('10', '2', expect.any(AbortSignal))
    toggle()
    expect(screen.queryByText('first thought')).toBeNull()
    toggle()
    expect(screen.getByText('first thought')).toBeTruthy()
    expect(query).toHaveBeenCalledTimes(1)
  })

  it('shows loading and does not duplicate an in-flight request', async () => {
    let resolve!: (value: ReasoningData) => void
    query.mockReturnValue(new Promise((done) => { resolve = done }))
    render(card())
    toggle()
    expect(screen.getByRole('status').textContent).toContain('正在读取')
    toggle(); toggle()
    expect(query).toHaveBeenCalledTimes(1)
    await act(async () => resolve(result))
    expect(screen.getByText('first thought')).toBeTruthy()
  })

  it('reports legacy or empty records without inventing content', async () => {
    query.mockResolvedValue({ message_id: '2', available: false, steps: [] })
    render(card())
    toggle()
    expect(await screen.findByText('这条回答没有保存可查看的思考记录')).toBeTruthy()
  })

  it('shows a safe failure and retries successfully', async () => {
    query.mockRejectedValueOnce(new Error('private server detail'))
    render(card())
    toggle()
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText('private server detail')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    expect(await screen.findByText('first thought')).toBeTruthy()
    expect(query).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('aborts on session change and ignores a stale response even if transport ignores abort', async () => {
    let resolve!: (value: ReasoningData) => void
    query.mockReturnValueOnce(new Promise((done) => { resolve = done }))
    const view = render(card())
    toggle()
    const signal = query.mock.calls[0][2]!
    view.rerender(card('20', '3'))
    expect(signal.aborted).toBe(true)
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false')
    await act(async () => resolve(result))
    expect(screen.queryByText('first thought')).toBeNull()
    toggle()
    await waitFor(() => expect(query).toHaveBeenLastCalledWith('20', '3', expect.any(AbortSignal)))
  })

  it('clears loaded content when moving to another message', async () => {
    const view = render(card())
    toggle()
    await screen.findByText('first thought')
    view.rerender(card('10', '3'))
    expect(screen.queryByText('first thought')).toBeNull()
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false')
    expect(query).toHaveBeenCalledTimes(1)
  })

  it('aborts on unmount/logout', () => {
    query.mockReturnValue(new Promise(() => {}))
    const view = render(card())
    toggle()
    const signal = query.mock.calls[0][2]!
    view.unmount()
    expect(signal.aborted).toBe(true)
  })

  it('supports English', async () => {
    window.localStorage.setItem('mentor-language', 'en')
    render(card())
    fireEvent.click(screen.getByRole('button', { name: 'View model reasoning' }))
    expect(await screen.findByText('Reasoning step 1')).toBeTruthy()
  })
})
