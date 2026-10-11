import { describe, expect, it } from 'vitest'

import type { StreamEvent } from '../types'
import { readEventStream } from './sse'

function fragmentedResponse(parts: string[]): Response {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

function byteFragmentedResponse(text: string, boundaries: number[]): Response {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({
    start(controller) {
      let start = 0
      for (const end of boundaries) {
        controller.enqueue(bytes.slice(start, end))
        start = end
      }
      controller.enqueue(bytes.slice(start))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

describe('readEventStream', () => {
  it('reassembles an event split across network chunks without breaking Chinese text', async () => {
    const events: StreamEvent[] = []
    const payload = [
      'event: message_start\ndata: {"session_id":"1","user_message_id":"2","attempt_id":"550e8400-e29b-41d4-a716-446655440000"}\r\n\r\n',
      'event: message_delta\ndata: {"attempt_id":"550e8400-e29b-41d4-a716-446655440000","text":"你好"}\r\n\r\n',
    ].join('')
    // 边界刻意落在 CRLF 和中文 UTF-8 字节内部，模拟真实网络任意切块。
    const response = byteFragmentedResponse(payload, [8, 121, 122, 124, 220, 221])

    await readEventStream(response, (event) => events.push(event))

    expect(events).toHaveLength(2)
    expect(events[1]).toMatchObject({ type: 'message_delta', data: { text: '你好' } })
  })

  it('ignores heartbeat comments and unknown event names', async () => {
    const events: StreamEvent[] = []
    const response = fragmentedResponse([
      ': ping\n\n',
      'event: internal_tool_call\ndata: {"secret":"hidden"}\n\n',
      'event: agent_progress\r\ndata: {"attempt_id":"550e8400-e29b-41d4-a716-446655440000","stage":"thinking"}\r\n\r\n',
    ])

    await readEventStream(response, (event) => events.push(event))

    expect(events).toEqual([{
      type: 'agent_progress',
      data: { attempt_id: '550e8400-e29b-41d4-a716-446655440000', stage: 'thinking' },
    }])
  })
})
