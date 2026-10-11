const FORWARDED_METHODS = new Set(['GET', 'POST', 'PATCH'])
const REQUEST_HEADERS_TO_REMOVE = [
  'connection',
  'content-length',
  'host',
  'transfer-encoding',
  'x-ai-proxy-secret',
  'x-client-ip',
  'x-forwarded-for',
  'x-forwarded-host',
  'x-forwarded-proto',
]
const RESPONSE_HEADERS_TO_REMOVE = ['connection', 'transfer-encoding']

function errorResponse(status, code, message) {
  return Response.json(
    { error: { code, message, request_id: crypto.randomUUID() } },
    { status, headers: { 'Cache-Control': 'no-store' } },
  )
}

function backendOrigin(value) {
  try {
    const parsed = new URL(value)
    if (
      parsed.protocol !== 'https:' ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== '/' ||
      parsed.search ||
      parsed.hash
    ) return null
    return parsed
  } catch {
    return null
  }
}

export async function onRequest({ request, env }) {
  if (!FORWARDED_METHODS.has(request.method)) {
    return errorResponse(405, 'METHOD_NOT_ALLOWED', 'This API method is not allowed.')
  }
  const origin = backendOrigin(env.BACKEND_ORIGIN)
  const secret = typeof env.PROXY_SHARED_SECRET === 'string' ? env.PROXY_SHARED_SECRET : ''
  const clientIp = request.headers.get('cf-connecting-ip')
  if (!origin || secret.length < 32 || !clientIp) {
    return errorResponse(503, 'SERVICE_UNAVAILABLE', 'The backend proxy is unavailable.')
  }

  const incoming = new URL(request.url)
  const target = new URL(incoming.pathname + incoming.search, origin)
  const headers = new Headers(request.headers)
  for (const name of REQUEST_HEADERS_TO_REMOVE) headers.delete(name)
  headers.set('x-ai-proxy-secret', secret)
  headers.set('x-client-ip', clientIp)

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === 'GET' ? undefined : request.body,
      redirect: 'manual',
    })
    const responseHeaders = new Headers(upstream.headers)
    for (const name of RESPONSE_HEADERS_TO_REMOVE) responseHeaders.delete(name)
    responseHeaders.set('Cache-Control', 'no-store')
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    })
  } catch {
    return errorResponse(503, 'SERVICE_UNAVAILABLE', 'The backend is temporarily unavailable.')
  }
}
