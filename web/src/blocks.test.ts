// A file goes up as blocks Blob Storage will accept, and the committed blocks are the file.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { BLOCK_BYTES, blockId, blockList, MAX_BLOCK_BYTES, MAX_BLOCKS, plan, range, remaining } from './blocks'
import { MAX_UPLOAD_BYTES } from './schema'

const bytes = fc.oneof(
  fc.integer({ min: 1, max: 4 * BLOCK_BYTES }),
  fc.integer({ min: 1, max: MAX_UPLOAD_BYTES }),
  fc.constantFrom(1, BLOCK_BYTES, BLOCK_BYTES + 1, MAX_BLOCKS * BLOCK_BYTES + 1, MAX_UPLOAD_BYTES),
)

describe('plan', () => {
  it("fits every size the API takes within Blob Storage's limits", () => {
    fc.assert(
      fc.property(bytes, (n) => {
        const p = plan(n)
        expect(p.count).toBeGreaterThanOrEqual(1)
        expect(p.count).toBeLessThanOrEqual(MAX_BLOCKS)
        expect(p.size).toBeGreaterThanOrEqual(BLOCK_BYTES)
        expect(p.size).toBeLessThanOrEqual(MAX_BLOCK_BYTES)
      }),
    )
  })

  it('cuts the file into consecutive, non-empty blocks that cover it exactly', () => {
    fc.assert(
      fc.property(bytes, fc.nat(), (n, pick) => {
        const p = plan(n)
        // Every block can't be checked for a terabyte; the first, the last and one between can.
        const indices = [0, pick % p.count, p.count - 1]
        for (const i of indices) {
          const [start, end] = range(p, n, i)
          expect(end).toBeGreaterThan(start)
          if (i > 0) expect(range(p, n, i - 1)[1]).toBe(start)
        }
        expect(range(p, n, 0)[0]).toBe(0)
        expect(range(p, n, p.count - 1)[1]).toBe(n)
      }),
    )
  })
})

describe('block ids', () => {
  it('are distinct and the same length, as Put Block requires', () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: MAX_BLOCKS - 1 }), fc.integer({ min: 0, max: MAX_BLOCKS - 1 }), (a, b) => {
        expect(blockId(a).length).toBe(blockId(b).length)
        expect(blockId(a) === blockId(b)).toBe(a === b)
      }),
    )
  })

  it('commit in order', () => {
    expect(blockList(2)).toBe(
      `<?xml version="1.0" encoding="utf-8"?><BlockList><Latest>${blockId(0)}</Latest><Latest>${blockId(1)}</Latest></BlockList>`,
    )
  })
})

describe('remaining', () => {
  it('is every block not yet staged, in order', () => {
    fc.assert(
      fc.property(fc.integer({ min: 1, max: 40 * BLOCK_BYTES }), fc.array(fc.nat({ max: 50 })), (n, staged) => {
        const p = plan(n)
        const left = remaining(p, new Set(staged))
        const expected = Array.from({ length: p.count }, (_, i) => i).filter((i) => !staged.includes(i))
        expect(left).toEqual(expected)
      }),
    )
  })
})
