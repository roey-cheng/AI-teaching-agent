import { afterEach, describe, expect, it, vi } from 'vitest'

import { onRequest } from './[[path]].js'

const env = {
  BACKEND_ORIGIN: 'https://backend.example.up.railway.app',
  PROXY_SHARED_SECRET: '0123456789abcdef0123456789abcdef',
}

afterEach(() => vi.unstubAllGlobals())

describe('Pages same-origin API proxy', () => {
  it('fails closed when deployment configuration is missing', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const response = await onRequest({
      request: new Request('https://ai.example.com/api/v1/users/me'),
      env: {},
    })
    expect(response.status).toBe(503)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('uses a fixed backend, strips spoofed headers and streams the response', async () => {
    const fetch = vi.fn(async (target, init) => {
      expect(String(target)).toBe(
        'https://backend.example.up.railway.app/api/v1/chat/sessions/7/messages'
      )
      expect(init.method).toBe('POST')
      expect(init.headers.get('x-ai-proxy-secret')).toBe(env.PROXY_SHARED_SECRET)
      expect(init.headers.get('x-client-ip')).toBe('203.0.113.8')
      expect(init.headers.get('x-forwarded-for')).toBeNull()
      expect(await new Response(init.body).text()).toBe('{"content":"hello"}')
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('event: message_delta\n'))
          controller.close()
        },
      })
      return new Response(stream, {
        headers: {
          'Content-Type': 'text/event-stream',
          'Set-Cookie': 'chat_session=test; HttpOnly; Secure',
        },
      })
    })
    vi.stubGlobal('fetch', fetch)
    const response = await onRequest({
      request: new Request('https://ai.example.com/api/v1/chat/sessions/7/messages', {
        method: 'POST',
        headers: {
          'cf-connecting-ip': '203.0.113.8',
          'content-type': 'application/json',
          'x-ai-proxy-secret': 'attacker-value',
          'x-forwarded-for': '198.51.100.1',
        },
        body: '{"content":"hello"}',
      }),
      env,
    })
    expect(response.status).toBe(200)
    expect(response.headers.get('cache-control')).toBe('no-store')
    expect(response.headers.get('set-cookie')).toContain('chat_session=test')
    expect(await response.text()).toBe('event: message_delta\n')
  })

  it('does not forward unsupported methods', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const response = await onRequest({
      request: new Request('https://ai.example.com/api/v1/users/me', {
        method: 'DELETE',
      }),
      env,
    })
    expect(response.status).toBe(405)
    expect(fetch).not.toHaveBeenCalled()
  })
})
