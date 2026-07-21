import { attachedImageNotice } from '../domain/messages.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { InputDetectDropResponse, PromptSubmitResponse } from '../gatewayTypes.js'
import type { Msg } from '../types.js'

import { turnController } from './turnController.js'
import { getUiState, patchUiState } from './uiStore.js'

const SESSION_BUSY_RE = /session busy|waiting for model response/i

export const isSessionBusyError = (error: unknown) =>
  error instanceof Error && SESSION_BUSY_RE.test(error.message)

export interface SubmitPromptDeps {
  appendMessage: (message: Msg) => void
  enqueue: (text: string) => void
  expand: (text: string) => string
  gw: GatewayClient
  maybeGoodVibes: (text: string) => void
  setLastUserMsg: (value: string) => void
  sys: (text: string) => void
}

export function markSubmitting(): void {
  patchUiState({ busy: true, status: 'running…' })
}

/** Submit a prompt after slash/shell routing has accepted it as model input. */
export function submitPrompt(text: string, deps: SubmitPromptDeps, showUserMessage = true): void {
  const sid = getUiState().sid

  if (!sid) {
    return deps.sys('session not ready yet')
  }

  // This must happen before input.detect_drop's first await. Otherwise two
  // back-to-back Enter presses both observe idle and race onto prompt.submit.
  markSubmitting()

  const startSubmit = (displayText: string, submitText: string, show = true) => {
    const liveSid = getUiState().sid

    if (!liveSid) {
      patchUiState({ busy: false, status: 'ready' })

      return deps.sys('session not ready yet')
    }

    turnController.clearStatusTimer()
    deps.maybeGoodVibes(submitText)
    deps.setLastUserMsg(text)

    if (show) {
      deps.appendMessage({ role: 'user', text: displayText })
    }

    patchUiState({ busy: true, status: 'running…' })
    turnController.bufRef = ''
    turnController.interrupted = false

    deps.gw
      .request<PromptSubmitResponse>('prompt.submit', { session_id: liveSid, text: submitText })
      .catch((error: Error) => {
        // A modern gateway accepts the complete prompt into its typed pending
        // queue. Keep this fallback for older gateways during rolling upgrades.
        if (isSessionBusyError(error)) {
          deps.enqueue(submitText)
          patchUiState({ busy: true, status: 'queued for next turn' })

          return deps.sys(`queued: "${submitText.slice(0, 50)}${submitText.length > 50 ? '…' : ''}"`)
        }

        deps.sys(`error: ${error.message}`)
        patchUiState({ busy: false, status: 'ready' })
      })
  }

  deps.gw
    .request<InputDetectDropResponse>('input.detect_drop', { session_id: sid, text })
    .then(result => {
      if (!result?.matched) {
        return startSubmit(text, deps.expand(text), showUserMessage)
      }

      if (result.is_image) {
        turnController.pushActivity(attachedImageNotice(result))
      } else {
        turnController.pushActivity(`detected file: ${result.name}`)
      }

      startSubmit(result.text || text, deps.expand(result.text || text), showUserMessage)
    })
    .catch(() => startSubmit(text, deps.expand(text), showUserMessage))
}
