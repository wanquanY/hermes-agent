import { useStdout } from '@hermes/ink'
import { useCallback, useEffect, useRef, useState } from 'react'

import type { PetGrid } from '../components/petSprite.js'

import { useGateway } from './gatewayContext.js'
import { $overlayState, getOverlayState } from './overlayStore.js'
import { $petFlash } from './petFlashStore.js'
import { $turnState } from './turnStore.js'
import { $uiState } from './uiStore.js'

export type PetState = 'failed' | 'idle' | 'jump' | 'review' | 'run' | 'waiting' | 'wave'

export const derivePetState = ({
  awaitingInput,
  busy,
  reasoning,
  toolRunning
}: {
  awaitingInput: boolean
  busy: boolean
  reasoning: boolean
  toolRunning: boolean
}): PetState => {
  if (awaitingInput) return 'waiting'
  if (toolRunning || busy) return 'run'
  if (reasoning) return 'review'
  return 'idle'
}

const awaitingInput = () => {
  const overlay = getOverlayState()
  return Boolean(overlay.clarify || overlay.approval || overlay.sudo || overlay.secret || overlay.confirm)
}

interface PetCellsResult {
  color?: string
  enabled?: boolean
  frameMs?: number
  frames?: PetGrid[] | string[]
  graphics?: string
  imageId?: number
  placeholder?: string[]
  scale?: number
  slug?: string
}

type CacheEntry =
  | { frameMs: number; frames: PetGrid[]; kind: 'cells' }
  | { color: string; frameMs: number; frames: string[]; kind: 'kitty'; placeholder: string[] }

export interface PetRender {
  enabled: boolean
  grid: PetGrid | null
  kitty: null | { color: string; placeholder: string[] }
}

const FRAME_MS = 160
const POLL_MS = 2500
const IS_TTY = Boolean(process.stdout?.isTTY)

export function usePet(): PetRender {
  const { rpc } = useGateway()
  const { stdout } = useStdout()
  const [enabled, setEnabled] = useState(false)
  const [grid, setGrid] = useState<PetGrid | null>(null)
  const [kitty, setKitty] = useState<PetRender['kitty']>(null)
  const [state, setState] = useState<PetState>('idle')
  const cache = useRef(new Map<string, CacheEntry>())
  const slug = useRef('')
  const scale = useRef(0)
  const imageId = useRef(0)
  const stateRef = useRef<PetState>('idle')
  const frame = useRef(0)

  useEffect(() => {
    let expiry: ReturnType<typeof setTimeout> | undefined
    const recompute = () => {
      clearTimeout(expiry)
      const flash = $petFlash.get()
      const now = Date.now()
      const next =
        flash && now < flash.until
          ? flash.state
          : derivePetState({
              awaitingInput: awaitingInput(),
              busy: $uiState.get().busy,
              reasoning: $turnState.get().reasoningActive,
              toolRunning: $turnState.get().tools.length > 0
            })
      if (flash && now < flash.until) expiry = setTimeout(recompute, flash.until - now)
      if (next !== stateRef.current) {
        stateRef.current = next
        frame.current = 0
        setState(next)
      }
    }
    recompute()
    const unsubscribers = [
      $turnState.listen(recompute),
      $uiState.listen(recompute),
      $petFlash.listen(recompute),
      $overlayState.listen(recompute)
    ]
    return () => {
      clearTimeout(expiry)
      unsubscribers.forEach(unsubscribe => unsubscribe())
    }
  }, [])

  const releaseKitty = useCallback(() => {
    if (!imageId.current) return
    try {
      stdout?.write(`\x1b_Ga=d,d=i,i=${imageId.current},q=2\x1b\\`)
    } catch {
      /* cosmetic */
    }
    imageId.current = 0
  }, [stdout])

  const sync = useCallback(
    async (petState: PetState) => {
      try {
        const result = (await rpc('pet.cells', {
          graphics: IS_TTY,
          session_id: $uiState.get().sid,
          state: petState
        })) as PetCellsResult | null
        if (!result) return
        if (!result.enabled) {
          releaseKitty()
          slug.current = ''
          cache.current.clear()
          setEnabled(false)
          setGrid(null)
          setKitty(null)
          return
        }
        const nextSlug = result.slug ?? ''
        const nextScale = result.scale ?? 0
        if (nextSlug !== slug.current || (nextScale > 0 && nextScale !== scale.current)) {
          releaseKitty()
          slug.current = nextSlug
          scale.current = nextScale
          cache.current.clear()
          frame.current = 0
        }
        if (result.graphics === 'kitty' && result.frames?.length && result.placeholder?.length) {
          imageId.current = result.imageId ?? 0
          cache.current.set(`${nextSlug}:${petState}`, {
            color: result.color ?? '#000001',
            frameMs: result.frameMs ?? FRAME_MS,
            frames: result.frames as string[],
            kind: 'kitty',
            placeholder: result.placeholder
          })
        } else if (result.frames?.length) {
          cache.current.set(`${nextSlug}:${petState}`, {
            frameMs: result.frameMs ?? FRAME_MS,
            frames: result.frames as PetGrid[],
            kind: 'cells'
          })
        }
        setEnabled(true)
      } catch {
        /* pet rendering is fail-open */
      }
    },
    [releaseKitty, rpc]
  )

  useEffect(() => {
    if (!cache.current.has(`${slug.current}:${state}`)) void sync(state)
    const timer = setInterval(() => void sync(stateRef.current), POLL_MS)
    return () => clearInterval(timer)
  }, [state, sync])

  useEffect(() => releaseKitty, [releaseKitty])

  useEffect(() => {
    if (!enabled) return
    const tick = () => {
      const entry = cache.current.get(`${slug.current}:${stateRef.current}`)
      if (!entry?.frames.length) return
      const index = frame.current % entry.frames.length
      frame.current = index + 1
      if (entry.kind === 'kitty') {
        try {
          stdout?.write(entry.frames[index] ?? '')
        } catch {
          /* cosmetic */
        }
        setGrid(null)
        setKitty({ color: entry.color, placeholder: entry.placeholder })
      } else {
        setKitty(null)
        setGrid(entry.frames[index] ?? null)
      }
    }
    tick()
    const timer = setInterval(tick, FRAME_MS)
    return () => clearInterval(timer)
  }, [enabled, state, stdout])

  return { enabled, grid, kitty }
}
