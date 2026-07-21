import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, resetUiState } from '../app/uiStore.js'

const ref = <T>(current: T) => ({ current })

const context = () => ({
  composer: { setInput: vi.fn() },
  gateway: { gw: { request: vi.fn() }, rpc: vi.fn(async () => null) },
  session: {
    STARTUP_RESUME_ID: '',
    colsRef: ref(80),
    newSession: vi.fn(),
    resetSession: vi.fn(),
    resumeById: vi.fn(),
    setCatalog: vi.fn()
  },
  submission: { submitRef: ref(vi.fn()) },
  system: { bellOnComplete: false, onReaction: vi.fn(), sys: vi.fn() },
  transcript: { appendMessage: vi.fn(), panel: vi.fn(), setHistoryItems: vi.fn() },
  voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
})

describe('gateway driver notifications', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
  })

  it('holds a busy-turn notice until message.complete', () => {
    const onEvent = createGatewayEventHandler(context() as any)

    onEvent({ type: 'message.start' })
    onEvent({
      payload: { key: 'credits.usage', level: 'warn', text: '⚠ Credits 90% used' },
      type: 'notification.show'
    })
    expect(getUiState().notice).toBeNull()

    onEvent({ payload: { text: 'done' }, type: 'message.complete' })
    expect(getUiState().notice).toMatchObject({ key: 'credits.usage', text: '⚠ Credits 90% used' })
  })

  it('uses an independent TTL and ignores a stale clear', () => {
    vi.useFakeTimers()
    try {
      const onEvent = createGatewayEventHandler(context() as any)

      onEvent({
        payload: { id: 'restored', key: 'credits.restored', kind: 'ttl', text: '✓ restored', ttl_ms: 8000 },
        type: 'notification.show'
      })
      onEvent({ payload: { key: 'credits.old' }, type: 'notification.clear' })
      expect(getUiState().notice?.id).toBe('restored')

      vi.advanceTimersByTime(8001)
      expect(getUiState().notice).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('projects reaction to both the heart callback and pet flash', () => {
    const ctx = context()
    const onEvent = createGatewayEventHandler(ctx as any)

    onEvent({ payload: { kind: 'vibe' }, type: 'reaction' })

    expect(ctx.system.onReaction).toHaveBeenCalledOnce()
  })
})
