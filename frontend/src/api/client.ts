import type {
  ChatSession,
  DuplicateMessageResponse,
  Memory,
  MessageHistory,
  StreamEvent,
  User,
} from '../types'
import type { Language } from '../i18n'
import { readEventStream } from './sse'

type ErrorBody = {
  error?: {
    code?: string
    message?: string
    request_id?: string
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

const zhErrorMessages: Record<string, string> = {
  AUTHENTICATION_REQUIRED: '登录状态已失效，请重新登录。',
  UNAUTHENTICATED: '登录状态已失效，请重新登录。',
  INVALID_CREDENTIALS: '邮箱或密码不正确。',
  EMAIL_ALREADY_REGISTERED: '这个邮箱已经注册过了。',
  REGISTRATION_UNAVAILABLE: '暂时无法注册，请稍后再试。',
  LOGIN_UNAVAILABLE: '暂时无法登录，请稍后再试。',
  AUTHENTICATION_UNAVAILABLE: '暂时无法检查登录状态，请稍后再试。',
  SESSION_BUSY: '这个对话仍在生成回答，请稍等。',
  SESSION_NOT_FOUND: '没有找到这个对话。',
  MESSAGE_NOT_FOUND: '没有找到这条消息。',
  RETRY_NOT_ALLOWED: '这条消息现在不能重试。',
  RATE_LIMITED: '发送得有点快，请稍后再试。',
  CONTEXT_TOO_LARGE: '这次对话内容太长，请新建一个对话后再试。',
  MODEL_CONFIGURATION_UNAVAILABLE: '聊天模型暂时不可用，请检查后端配置。',
  SERVER_SHUTTING_DOWN: '后端正在重启，请稍后再试。',
  GENERATION_TIMEOUT: '回答生成超时了，可以再试一次。',
  GENERATION_INTERRUPTED: '回答生成被中断了，可以再试一次。',
  MODEL_REQUEST_FAILED: '模型暂时没有成功回答，请稍后再试。',
  GENERATION_FAILED: '回答生成失败了，可以再试一次。',
  INVALID_STREAM: '后端没有返回预期的聊天数据流。',
  HTTP_ERROR: '请求失败，请稍后重试。',
}

const enErrorMessages: Record<string, string> = {
  AUTHENTICATION_REQUIRED: 'Your session has expired. Please log in again.',
  UNAUTHENTICATED: 'Your session has expired. Please log in again.',
  INVALID_CREDENTIALS: 'The email or password is incorrect.',
  EMAIL_ALREADY_REGISTERED: 'An account already exists for this email.',
  REGISTRATION_UNAVAILABLE: 'Sign-up is temporarily unavailable. Please try again.',
  LOGIN_UNAVAILABLE: 'Login is temporarily unavailable. Please try again.',
  AUTHENTICATION_UNAVAILABLE: 'Unable to check your session. Please try again.',
  SESSION_BUSY: 'This chat is still generating a response.',
  SESSION_NOT_FOUND: 'This conversation could not be found.',
  MESSAGE_NOT_FOUND: 'This message could not be found.',
  RETRY_NOT_ALLOWED: 'This response cannot be retried now.',
  RATE_LIMITED: 'You are sending messages too quickly. Please wait a moment.',
  CONTEXT_TOO_LARGE: 'This conversation is too long. Start a new chat and try again.',
  MODEL_CONFIGURATION_UNAVAILABLE: 'The chat model is unavailable. Check the server configuration.',
  SERVER_SHUTTING_DOWN: 'The server is restarting. Please try again shortly.',
  GENERATION_TIMEOUT: 'Response generation timed out. You can retry it.',
  GENERATION_INTERRUPTED: 'Response generation was interrupted. You can retry it.',
  MODEL_REQUEST_FAILED: 'The model could not answer right now. Please try again.',
  GENERATION_FAILED: 'Response generation failed. You can retry it.',
  INVALID_STREAM: 'The server did not return the expected chat stream.',
  HTTP_ERROR: 'The request failed. Please try again.',
}

export function friendlyErrorMessage(code: string, fallback: string, language: Language = 'zh'): string {
  return (language === 'zh' ? zhErrorMessages : enErrorMessages)[code] ?? fallback
}

export function userFacingError(error: unknown, language: Language = 'zh'): string {
  if (error instanceof ApiError) {
    return friendlyErrorMessage(error.code, error.message, language)
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return language === 'zh' ? '请求已取消。' : 'The request was cancelled.'
  }
  return error instanceof Error ? error.message : language === 'zh'
    ? '发生了未知错误，请稍后重试。'
    : 'Something went wrong. Please try again.'
}

async function apiError(response: Response): Promise<ApiError> {
  let body: ErrorBody = {}
  try {
    body = (await response.json()) as ErrorBody
  } catch {
    // 非 JSON 错误也统一转成安全、可展示的错误。
  }
  const error = new ApiError(
    response.status,
    body.error?.code ?? 'HTTP_ERROR',
    body.error?.message ?? `请求失败（HTTP ${response.status}）`,
    body.error?.request_id,
  )
  if (response.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('mentor:authentication-expired'))
  }
  return error
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...init.headers,
    },
  })
  if (!response.ok) throw await apiError(response)
  return (await response.json()) as T
}

export const accountApi = {
  me: () => requestJson<User>('/api/v1/users/me'),
  register: (input: { email: string; password: string; display_name: string }) =>
    requestJson<User>('/api/v1/auth/register', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  login: (input: { email: string; password: string }) =>
    requestJson<{ user: User }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  logout: async () => {
    const response = await fetch('/api/v1/auth/logout', {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!response.ok) throw await apiError(response)
  },
}

export const sessionApi = {
  list: async () => (await requestJson<{ items: ChatSession[] }>('/api/v1/chat/sessions')).items,
  create: () =>
    requestJson<ChatSession>('/api/v1/chat/sessions', {
      method: 'POST',
      body: '{}',
    }),
  rename: (sessionId: string, title: string) =>
    requestJson<ChatSession>(`/api/v1/chat/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  history: (sessionId: string) =>
    requestJson<MessageHistory>(`/api/v1/chat/sessions/${sessionId}/messages`),
}

export const memoryApi = {
  list: async () => (await requestJson<{ items: Memory[] }>('/api/v1/me/memory')).items,
}

export async function streamChat(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<DuplicateMessageResponse | null> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    signal,
    headers: {
      Accept: 'text/event-stream, application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) throw await apiError(response)
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    return (await response.json()) as DuplicateMessageResponse
  }
  if (!contentType.includes('text/event-stream')) {
    throw new ApiError(502, 'INVALID_STREAM', '后端没有返回预期的聊天数据流。')
  }

  await readEventStream(response, onEvent)
  return null
}
