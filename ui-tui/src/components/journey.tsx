import { Box, NoSelect, Text, useInput, useStdout } from '@hermes/ink'
import { useEffect, useMemo, useState } from 'react'

import type { GatewayClient } from '../gatewayClient.js'
import { openInEditor } from '../lib/editor.js'
import { rpcErrorMessage } from '../lib/rpc.js'
import type { Theme } from '../theme.js'

type Run = [string, string, number?, (string | null)?]

interface JourneyNode {
  body?: string
  fullLabel?: string
  glyph: string
  id: string
  label: string
  meta: string
  style: string
}

interface JourneyBucket {
  date: string
  label: string
  nodes: JourneyNode[]
}

interface JourneyFrames {
  buckets?: JourneyBucket[]
  frames: { grid: Run[][] }[]
  summary: string[]
}

interface MutationResult {
  content?: string
  kind?: string
  message: string
  ok: boolean
}

interface JourneyProps {
  gw: GatewayClient
  onClose: () => void
  sessionId: null | string
  t: Theme
}

const runColor = (run: Run, t: Theme) => {
  if (run[3]) return run[3]
  if (run[1] === 'skill') return t.color.accent
  if (run[1] === 'memory') return t.color.primary
  if (run[1] === 'dim') return t.color.muted
  return t.color.text
}

export function Journey({ gw, onClose, sessionId, t }: JourneyProps) {
  const { stdout } = useStdout()
  const cols = Math.max(40, (stdout?.columns ?? 90) - 4)
  const chartRows = Math.max(5, Math.min(9, Math.floor((stdout?.rows ?? 30) * 0.3)))
  const [data, setData] = useState<JourneyFrames | null>(null)
  const [cursor, setCursor] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let active = true
    setError('')
    gw.request<JourneyFrames>('learning.frames', {
      cols,
      frames: 2,
      rows: chartRows,
      session_id: sessionId
    })
      .then(result => {
        if (!active) return
        setData(result)
        const count = (result?.buckets ?? []).reduce((n, bucket) => n + bucket.nodes.length, 0)
        setCursor(Math.max(0, count - 1))
      })
      .catch((reason: unknown) => active && setError(rpcErrorMessage(reason)))
    return () => {
      active = false
    }
  }, [chartRows, cols, gw, reload, sessionId])

  const rows = useMemo(
    () => (data?.buckets ?? []).flatMap(bucket => bucket.nodes.map(node => ({ bucket, node }))),
    [data]
  )
  const selected = rows[Math.min(cursor, Math.max(0, rows.length - 1))]

  const editSelected = async () => {
    if (!selected) return
    try {
      const detail = await gw.request<MutationResult>('learning.detail', {
        id: selected.node.id,
        session_id: sessionId
      })
      if (!detail.ok || detail.content == null) {
        return setNotice(detail.message || 'cannot edit this item')
      }
      const edited = await openInEditor(detail.content, detail.kind === 'skill' ? '.md' : '.txt')
      if (edited == null || edited.trim() === detail.content.trim()) {
        return setNotice('no changes')
      }
      const result = await gw.request<MutationResult>('learning.edit', {
        content: edited,
        id: selected.node.id,
        session_id: sessionId
      })
      setNotice(result.message)
      if (result.ok) setReload(value => value + 1)
    } catch (reason) {
      setNotice(rpcErrorMessage(reason))
    }
  }

  const deleteSelected = async () => {
    if (!selected) return
    try {
      const result = await gw.request<MutationResult>('learning.delete', {
        id: selected.node.id,
        session_id: sessionId
      })
      setNotice(result.message)
      if (result.ok) setReload(value => value + 1)
    } catch (reason) {
      setNotice(rpcErrorMessage(reason))
    } finally {
      setConfirmDelete(false)
    }
  }

  useInput((input, key) => {
    if (key.escape || input === 'q') return onClose()
    if (key.upArrow || input === 'k') {
      setConfirmDelete(false)
      return setCursor(value => Math.max(0, value - 1))
    }
    if (key.downArrow || input === 'j') {
      setConfirmDelete(false)
      return setCursor(value => Math.min(Math.max(0, rows.length - 1), value + 1))
    }
    if (input === 'e' && selected) void editSelected()
    if (input === 'd' && selected) {
      if (confirmDelete) void deleteSelected()
      else {
        setConfirmDelete(true)
        setNotice('press d again to delete; skills are archived and recoverable')
      }
    }
  })

  const latest = data?.frames.at(-1)?.grid ?? []

  return (
    <NoSelect>
      <Box flexDirection="column" paddingX={1}>
        <Box justifyContent="space-between">
          <Text bold color={t.color.primary}>
            ✦ Journey
          </Text>
          <Text color={t.color.muted}>↑↓ move · e edit · d delete · esc close</Text>
        </Box>
        {error ? <Text color={t.color.error}>{error}</Text> : null}
        {!data && !error ? <Text color={t.color.muted}>loading learning graph…</Text> : null}
        {latest.map((row, index) => (
          <Text key={index}>
            {row.map((run, runIndex) => (
              <Text color={runColor(run, t)} key={runIndex}>
                {run[0]}
              </Text>
            ))}
          </Text>
        ))}
        <Box marginTop={1} flexDirection="column">
          {rows.length ? (
            rows.slice(Math.max(0, cursor - 5), cursor + 6).map((row, index) => {
              const absolute = Math.max(0, cursor - 5) + index
              const active = absolute === cursor
              return (
                <Text bold={active} color={active ? t.color.accent : t.color.text} inverse={active} key={row.node.id}>
                  {`${active ? '›' : ' '} ${row.node.glyph} ${row.node.label} · ${row.bucket.label || row.bucket.date} · ${row.node.meta}`}
                </Text>
              )
            })
          ) : data ? (
            <Text color={t.color.muted}>No learned skills or memories yet.</Text>
          ) : null}
        </Box>
        {selected?.node.body ? <Text color={t.color.muted}>{selected.node.body}</Text> : null}
        {notice ? <Text color={confirmDelete ? t.color.warn : t.color.muted}>{notice}</Text> : null}
      </Box>
    </NoSelect>
  )
}
