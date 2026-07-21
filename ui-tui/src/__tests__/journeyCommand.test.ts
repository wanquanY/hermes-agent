import { beforeEach, describe, expect, it } from 'vitest'

import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { findSlashCommand } from '../app/slash/registry.js'

describe('/journey slash command', () => {
  beforeEach(() => resetOverlayState())

  it('resolves by name and aliases', () => {
    expect(findSlashCommand('journey')?.name).toBe('journey')
    for (const alias of ['learning', 'memory-graph']) {
      expect(findSlashCommand(alias)?.name).toBe('journey')
    }
  })

  it('opens a persistent journey overlay', async () => {
    findSlashCommand('journey')!.run('', {} as never, 'journey')
    expect(getOverlayState().journey).toBe(true)
    const { resetFlowOverlays } = await import('../app/overlayStore.js')
    resetFlowOverlays()
    expect(getOverlayState().journey).toBe(true)
  })
})
