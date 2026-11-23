import { expect, it } from 'vitest'

import { applyGenerationEvent } from './liveGeneration'
import type { LiveGeneration } from './types'

function generation(): LiveGeneration {
  return {
    sessionId: '10', mode: 'send', userContent: 'question', attemptId: 'attempt',
    stage: 'agent_running', completionStatus: null, reasoning: '', answer: '', error: null,
  }
}

it('keeps assistant text before tools separate from the final answer', () => {
  let live = applyGenerationEvent(generation(), {
    type: 'message_delta', data: { attempt_id: 'attempt', text: 'I will check your saved preference.' },
  })
  live = applyGenerationEvent(live, {
    type: 'model_step', data: { attempt_id: 'attempt', step: 1, phase: 'tool' },
  })
  expect(live.intermediateAssistantMessages).toEqual(['I will check your saved preference.'])
  expect(live.answer).toBe('')
  live = applyGenerationEvent(live, {
    type: 'model_step', data: { attempt_id: 'attempt', step: 2, phase: 'start' },
  })
  live = applyGenerationEvent(live, {
    type: 'message_delta', data: { attempt_id: 'attempt', text: 'Here is your answer.' },
  })
  live = applyGenerationEvent(live, {
    type: 'model_step', data: { attempt_id: 'attempt', step: 2, phase: 'final' },
  })
  expect(live.answer).toBe('Here is your answer.')
  expect(live.intermediateAssistantMessages).toEqual(['I will check your saved preference.'])
})

it('distinguishes the server saving stage from confirmed persistence', () => {
  const saving = applyGenerationEvent(generation(), {
    type: 'agent_progress', data: { attempt_id: 'attempt', stage: 'saving' },
  })
  expect(saving.stage).toBe('saving')
  expect(saving.completionStatus).toBeNull()
  const saved = applyGenerationEvent(saving, {
    type: 'message_done', data: {
      attempt_id: 'attempt', assistant_message: {
        message_id: '2', role: 'ASSISTANT', content: 'answer',
        in_reply_to_message_id: '1', created_at: '2026-10-17T00:00:00Z',
      },
    },
  })
  expect(saved.stage).toBeNull()
  expect(saved.completionStatus).toBe('saved')
})

it('does not claim that failed generation was saved', () => {
  const failed = applyGenerationEvent(generation(), {
    type: 'message_error', data: {
      attempt_id: 'attempt', error: { code: 'TEST_FAILURE', message: 'Failed', request_id: 'request' },
    },
  })
  expect(failed.completionStatus).toBeNull()
  expect(failed.error?.code).toBe('TEST_FAILURE')
})
