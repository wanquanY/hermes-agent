import { Box, Text } from '@hermes/ink'
import { memo } from 'react'

export type PetCell = number[]
export type PetGrid = PetCell[][]

const hex = (r: number, g: number, b: number) =>
  `#${[r, g, b]
    .map(value =>
      Math.max(0, Math.min(255, value | 0))
        .toString(16)
        .padStart(2, '0')
    )
    .join('')}`

export const PetSprite = memo(function PetSprite({ grid }: { grid: PetGrid }) {
  if (!grid.length) return null
  return (
    <Box flexDirection="column">
      {grid.map((row, y) => (
        <Box key={y}>
          {row.map((cell, x) => {
            const [tr, tg, tb, ta, br, bg, bb, ba] = cell
            const top = (ta ?? 0) >= 32
            const bottom = (ba ?? 0) >= 32
            if (!top && !bottom) return <Text key={x}> </Text>
            if (top && bottom) {
              return (
                <Text backgroundColor={hex(br, bg, bb)} color={hex(tr, tg, tb)} key={x}>
                  ▀
                </Text>
              )
            }
            return top ? (
              <Text color={hex(tr, tg, tb)} key={x}>
                ▀
              </Text>
            ) : (
              <Text color={hex(br, bg, bb)} key={x}>
                ▄
              </Text>
            )
          })}
        </Box>
      ))}
    </Box>
  )
})

export const PetKitty = memo(function PetKitty({ color, placeholder }: { color: string; placeholder: string[] }) {
  if (!placeholder.length) return null
  return (
    <Box flexDirection="column">
      {placeholder.map((row, y) => (
        <Text color={color} key={y}>
          {row}
        </Text>
      ))}
    </Box>
  )
})
