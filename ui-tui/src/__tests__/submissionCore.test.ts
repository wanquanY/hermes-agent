import { beforeEach, describe, expect, it, vi } from 'vitest'

import { markSubmitting, submitPrompt, type SubmitPromptDeps } from '../app/submissionCore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { GatewayClient } from '../gatewayClient.js'

function deferredGateway() {
  let resolveDrop!: (value: unknown) => void

  const drop = new Promise(resolve => {
    resolveDrop = resolve
  })

  const calls: string[] = []

  const gw = {
    request: vi.fn((method: string) => {
      calls.push(method)

      return method === 'input.detect_drop'
        ? drop
        : Promise.resolve({ status: 'streaming' })
    })
  } as unknown as GatewayClient

  return { calls, gw, resolveDrop }
}

function deps(gw: GatewayClient, overrides: Partial<SubmitPromptDeps> = {}): SubmitPromptDeps {
  return {
    appendMessage: vi.fn(),
    enqueue: vi.fn(),
    expand: text => text,
    gw,
    maybeGoodVibes: vi.fn(),
    setLastUserMsg: vi.fn(),
    sys: vi.fn(),
    ...overrides
  }
}

describe('submitPrompt synchronous busy ownership', () => {
  beforeEach(() => {
    resetUiState()
    patchUiState({ sid: 'session-1' })
  })

  it('marks busy before file-drop detection resolves', () => {
    const { gw } = deferredGateway()

    submitPrompt('hello', deps(gw))

    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')
  })

  it('makes a back-to-back submit observe the first synchronous claim', () => {
    const { gw } = deferredGateway()

    submitPrompt('first', deps(gw))

    expect(getUiState().busy).toBe(true)
  })

  it('does not claim busy without a live session', () => {
    resetUiState()
    const { calls, gw } = deferredGateway()
    const sys = vi.fn()

    submitPrompt('hello', deps(gw, { sys }))

    expect(getUiState().busy).toBe(false)
    expect(sys).toHaveBeenCalledWith('session not ready yet')
    expect(calls).toEqual([])
  })

  it('submits after file-drop detection reports no match', async () => {
    const { calls, gw, resolveDrop } = deferredGateway()

    submitPrompt('hello', deps(gw))
    resolveDrop({ matched: false })
    await Promise.resolve()
    await Promise.resolve()

    expect(calls).toEqual(['input.detect_drop', 'prompt.submit'])
  })
})

describe('markSubmitting', () => {
  it('owns busy and running status together', () => {
    resetUiState()

    markSubmitting()

    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')
  })
})
