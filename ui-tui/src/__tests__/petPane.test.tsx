import { PassThrough } from 'stream'

import { Box, renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { usePet } from '../app/usePet.js'
import { PetPane } from '../components/appLayout.js'
import { stripAnsi } from '../lib/text.js'

vi.mock('../app/usePet.js', () => ({ usePet: vi.fn() }))

const opaqueCell = [255, 0, 0, 255, 0, 0, 255, 255]
const glyphs = new Set(['▀', '▄', '█'])
const firstGlyphColumn = (line: string) => [...line].findIndex(ch => glyphs.has(ch))

const renderFrame = (element: React.ReactElement, columns = 40) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''
  Object.assign(stdout, { columns, isTTY: false, rows: 12 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })
  const instance = renderSync(element, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })
  instance.unmount()
  instance.cleanup()
  return stripAnsi(output)
    .split('\n')
    .map(line => line.replace(/\s+$/, ''))
}

describe('PetPane', () => {
  afterEach(() => vi.mocked(usePet).mockReset())

  it('overlays a flat right-aligned sprite', () => {
    vi.mocked(usePet).mockReturnValue({
      enabled: true,
      grid: [
        [opaqueCell, opaqueCell],
        [opaqueCell, opaqueCell]
      ],
      kitty: null
    })
    const lines = renderFrame(
      <Box flexDirection="column" height={8} position="relative" width={40}>
        <PetPane />
      </Box>
    )
    const columns = lines.map(firstGlyphColumn).filter(column => column >= 0)
    expect(columns.length).toBeGreaterThanOrEqual(2)
    expect(new Set(columns).size).toBe(1)
    expect(columns[0]).toBeGreaterThan(20)
  })

  it('renders nothing when disabled', () => {
    vi.mocked(usePet).mockReturnValue({ enabled: false, grid: null, kitty: null })
    const lines = renderFrame(
      <Box flexDirection="column" height={8} position="relative" width={40}>
        <PetPane />
      </Box>
    )
    expect(lines.every(line => firstGlyphColumn(line) < 0)).toBe(true)
  })
})
