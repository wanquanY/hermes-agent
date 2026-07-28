import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClient.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

import { OverlayHint, windowItems } from './overlayControls.js'

interface GalleryPet {
  curated?: boolean
  displayName: string
  installed: boolean
  slug: string
}
interface Gallery {
  active: string
  enabled: boolean
  pets: GalleryPet[]
}

export function PetPicker({
  gw,
  onClose,
  sessionId,
  t
}: {
  gw: GatewayClient
  onClose: () => void
  sessionId: null | string
  t: Theme
}) {
  const [gallery, setGallery] = useState<Gallery | null>(null)
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { stdout } = useStdout()
  const width = Math.max(40, Math.min(90, (stdout?.columns ?? 80) - 6))

  useEffect(() => {
    gw.request<Gallery>('pet.gallery', { session_id: sessionId })
      .then(result => setGallery(result))
      .catch((reason: unknown) => setError(rpcErrorMessage(reason)))
  }, [gw, sessionId])

  const view = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const pets = (gallery?.pets ?? []).filter(
      pet =>
        !/^clawd(-|$)/i.test(pet.slug) &&
        (!needle || pet.slug.toLowerCase().includes(needle) || pet.displayName.toLowerCase().includes(needle))
    )
    const rank = (pet: GalleryPet) =>
      (gallery?.enabled && pet.slug === gallery.active ? 4 : 0) + (pet.installed ? 2 : 0) + (pet.curated ? 1 : 0)
    return [...pets].sort((a, b) => rank(b) - rank(a))
  }, [gallery, query])

  const adopt = (slug: string) => {
    setBusy(true)
    gw.request('pet.select', { session_id: sessionId, slug })
      .then(() => onClose())
      .catch((reason: unknown) => {
        setError(rpcErrorMessage(reason))
        setBusy(false)
      })
  }

  useInput((input, key) => {
    if (busy) return
    if (key.escape) return onClose()
    if (key.upArrow) return setIndex(value => Math.max(0, value - 1))
    if (key.downArrow) return setIndex(value => Math.min(view.length - 1, value + 1))
    if (key.return) return view[index] ? adopt(view[index]!.slug) : undefined
    if (key.backspace || key.delete) {
      setQuery(value => value.slice(0, -1))
      return setIndex(0)
    }
    if (input && input.length === 1 && input >= ' ' && !key.ctrl && !key.meta) {
      setQuery(value => value + input)
      setIndex(0)
    }
  })

  if (!gallery && !error) return <Text color={t.color.muted}>loading pets…</Text>
  const { items, offset } = windowItems(view, index, 10)
  return (
    <Box flexDirection="column" width={width}>
      <Text bold color={t.color.accent}>
        Pets
      </Text>
      <Text color={t.color.muted}>
        {query ? `filter: ${query}` : 'type to filter'} · {view.length} pets
      </Text>
      {items.map((pet, itemIndex) => {
        const active = offset + itemIndex === index
        const selected = gallery?.enabled && pet.slug === gallery.active
        return (
          <Text bold={active} color={active ? t.color.accent : t.color.muted} inverse={active} key={pet.slug}>
            {active ? '▸ ' : '  '}
            {selected ? '●' : pet.installed ? '✓' : ' '} {pet.displayName} ({pet.slug})
          </Text>
        )
      })}
      {!view.length ? <Text color={t.color.muted}>no pets match</Text> : null}
      {error ? <Text color={t.color.error}>error: {error}</Text> : null}
      {busy ? <Text color={t.color.accent}>adopting…</Text> : null}
      <OverlayHint t={t}>↑/↓ select · Enter adopt · type to filter · Esc cancel</OverlayHint>
    </Box>
  )
}
