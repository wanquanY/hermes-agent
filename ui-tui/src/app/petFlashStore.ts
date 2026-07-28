import { atom } from 'nanostores'

import type { PetState } from './usePet.js'

interface PetFlash {
  state: PetState
  until: number
}

export const $petFlash = atom<PetFlash | null>(null)
export const flashPet = (state: PetState, ms = 1600) => $petFlash.set({ state, until: Date.now() + ms })
