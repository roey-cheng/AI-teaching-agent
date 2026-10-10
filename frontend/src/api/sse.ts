import type { StreamEvent } from '../types'

const eventNames = new Set<StreamEvent['type']>([
  'message_start',
  'agent_progress',
  'reasoning_delta',
  'message_delta',
  'message_done',
  'message_error',
])

function parseFrame(frame: string): StreamEvent | null {
  let eventName = ''
  const dataLines: string[] = []

  for (const line of frame.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }

  if (!eventNames.has(eventName as StreamEvent['type']) || dataLines.length === 0) return null
  return {
    type: eventName,
    data: JSON.parse(dataLines.join('\n')),
  } as StreamEvent
}

export async function readEventStream(
  response: Response,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  if (!response.body) throw new Error('浏览器没有提供可读取的响应流。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { value, done } = await reader.read()
    // 先拼接再统一 CRLF，才能处理“\r”和“\n”恰好落在两个网络分片的情况。
    buffer = (buffer + decoder.decode(value, { stream: !done })).replaceAll('\r\n', '\n')

    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const event = parseFrame(frame)
      if (event) onEvent(event)
      boundary = buffer.indexOf('\n\n')
    }

    if (done) break
  }

  const finalEvent = parseFrame(buffer)
  if (finalEvent) onEvent(finalEvent)
}
