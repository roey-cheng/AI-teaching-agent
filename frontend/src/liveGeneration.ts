import type { LiveGeneration, StreamEvent } from './types'

export function applyGenerationEvent(current: LiveGeneration, event: StreamEvent): LiveGeneration {
  switch (event.type) {
    case 'model_step': return {
      ...current,
      answer: event.data.phase === 'final' ? current.answer : '',
      // This is assistant text preceding a tool call, not a tool name or result.
      intermediateAssistantMessages: event.data.phase === 'tool' && current.answer
        ? [...(current.intermediateAssistantMessages ?? []), current.answer]
        : current.intermediateAssistantMessages,
    }
    case 'message_start': return { ...current, attemptId: event.data.attempt_id }
    case 'agent_progress': return { ...current, attemptId: event.data.attempt_id, stage: event.data.stage }
    case 'reasoning_delta': return { ...current, attemptId: event.data.attempt_id, reasoning: current.reasoning + event.data.text }
    case 'message_delta': return { ...current, attemptId: event.data.attempt_id, answer: current.answer + event.data.text }
    case 'message_done': return { ...current, attemptId: event.data.attempt_id, stage: null, completionStatus: 'saved' }
    case 'message_error': return { ...current, attemptId: event.data.attempt_id, error: event.data.error }
  }
}
