import { beforeEach, describe, expect, it } from 'vitest'

import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

describe('turnController flash-and-yield notices', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
  })

  it.each(['credits.usage', 'credits.grant_spent'])('clears %s when a new turn starts', key => {
    patchUiState({ notice: { key, kind: 'sticky', level: 'warn', text: 'heads up' } })

    turnController.startMessage()

    expect(getUiState().notice).toBeNull()
  })

  it('keeps depletion sticky across a new turn', () => {
    patchUiState({
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ access paused' }
    })

    turnController.startMessage()

    expect(getUiState().notice?.key).toBe('credits.depleted')
  })
})
