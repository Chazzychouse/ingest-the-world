// ACC-3: the banner shows the highest marking on screen, whatever order the data arrives in.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import type { Collection } from './api'
import { bannerFor, UNCLASSIFIED } from './markings'

const MARKINGS = [
  { marking: 'UNCLASSIFIED', marking_rank: 0, marking_color: '#007a33' },
  { marking: 'FOUO', marking_rank: 5, marking_color: '#333333' },
  { marking: 'CUI', marking_rank: 10, marking_color: '#502b85' },
]

const collection = fc.record({ id: fc.string(), level: fc.constantFrom(...MARKINGS) }).map(
  ({ id, level }): Collection => ({
    id,
    title: id,
    description: '',
    ...level,
    marking_label: level.marking,
    cui_categories: [],
    dissemination: [],
    retention: { retain_days: null, legal_hold: false, tiering: [] },
    created_by: 'test',
    created_at: '2026-01-01T00:00:00Z',
  }),
)

describe('bannerFor [ACC-3]', () => {
  it('is UNCLASSIFIED for an empty screen', () => {
    expect(bannerFor([])).toEqual(UNCLASSIFIED)
  })

  it('shows the highest-ranked marking regardless of order', () => {
    fc.assert(
      fc.property(fc.array(collection, { minLength: 1 }), (collections) => {
        const top = Math.max(...collections.map((c) => c.marking_rank))
        const banner = bannerFor(collections)
        expect(MARKINGS.find((m) => m.marking === banner.code)?.marking_rank).toBe(top)
        expect(bannerFor(collections.toReversed())).toEqual(banner)
      }),
    )
  })
})
