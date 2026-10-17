import { afterEach, expect, it, vi } from 'vitest'

import { sessionApi } from './client'

afterEach(() => vi.unstubAllGlobals())

it('fetches the message-scoped endpoint with authentication, no-store and cancellation', async () => {
  const data = { message_id: '2', available: false, steps: [] }
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(data)))
  vi.stubGlobal('fetch', fetchMock)
  const controller = new AbortController()
  expect(await sessionApi.reasoning('10', '2', controller.signal)).toEqual(data)
  expect(fetchMock).toHaveBeenCalledWith('/api/v1/chat/sessions/10/messages/2/reasoning', {
    signal: controller.signal,
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
})
