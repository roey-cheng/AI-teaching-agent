export type User = {
  user_id: string
  email: string
  display_name: string
}

export type ChatSession = {
  session_id: string
  title: string
  created_at: string
  updated_at: string
  last_activity_at: string
}

export type GenerationError = {
  code: string
  message: string
}

export type Generation = {
  attempt_id: string
  status: 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  assistant_message_id: string | null
  error: GenerationError | null
  can_retry: boolean
}

export type UserMessage = {
  message_id: string
  role: 'USER'
  content: string
  in_reply_to_message_id: null
  created_at: string
  generation: Generation
}

export type AssistantMessage = {
  message_id: string
  role: 'ASSISTANT'
  content: string
  in_reply_to_message_id: string
  created_at: string
}

export type Message = UserMessage | AssistantMessage

export type MessageHistory = {
  session_id: string
  is_generating: boolean
  items: Message[]
}

export type MemoryType =
  | 'LEARNING_PREFERENCE'
  | 'LEARNING_GOAL'
  | 'PROGRAMMING_BACKGROUND'
  | 'PERSONAL_BACKGROUND'
  | 'DAILY_PREFERENCE'

export type Memory = {
  memory_id: string
  memory_type: MemoryType
  summary: string
  updated_at: string
}

export type DuplicateMessageResponse = {
  duplicate: true
  session_id: string
  user_message_id: string
  attempt_id: string
  status: Generation['status']
  assistant_message_id: string | null
}

export type AgentStage =
  | 'context_ready'
  | 'agent_running'
  | 'thinking'
  | 'answering'
  | 'saving'
  | 'memory_checking'
  | 'memory_saving'
  | 'memory_saved'
  | 'memory_skipped'
  | 'memory_unavailable'

export type StreamEvent =
  | { type: 'message_start'; data: { session_id: string; user_message_id: string; attempt_id: string } }
  | { type: 'agent_progress'; data: { attempt_id: string; stage: AgentStage } }
  | { type: 'reasoning_delta'; data: { attempt_id: string; text: string } }
  | { type: 'message_delta'; data: { attempt_id: string; text: string } }
  | { type: 'message_done'; data: { attempt_id: string; assistant_message: AssistantMessage } }
  | {
      type: 'message_error'
      data: { attempt_id: string; error: GenerationError & { request_id: string } }
    }

export type LiveGeneration = {
  sessionId: string
  mode: 'send' | 'retry'
  userContent: string
  attemptId: string | null
  stage: AgentStage | null
  reasoning: string
  answer: string
  error: GenerationError | null
}
