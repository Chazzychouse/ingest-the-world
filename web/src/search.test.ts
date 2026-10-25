// SRCH-1 starts in the browser: the bbox sent for a viewport must cover exactly what's on screen.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { nextPage, searchBody, viewportBbox, type Bbox } from './search'

/** Whether a STAC bbox covers longitude `lon` (in [-180, 180]). */
function covers([west, , east]: Bbox, lon: number): boolean {
  return west <= east ? west <= lon && lon <= east : lon >= west || lon <= east
}

const wrap = (lon: number) => ((((lon + 180) % 360) + 360) % 360) - 180

// MapLibre viewports: anywhere on a world wrapped a few times, narrower than one world.
const viewport = fc
  .record({ west: fc.integer({ min: -900, max: 900 }), width: fc.integer({ min: 1, max: 359 }) })
  .map(({ west, width }) => ({ west, east: west + width }))

describe('viewportBbox [SRCH-1]', () => {
  it('is in range, with west > east only across the antimeridian', () => {
    fc.assert(
      fc.property(viewport, ({ west, east }) => {
        const [w, , e] = viewportBbox(west, -10, east, 10)
        expect(w).toBeGreaterThanOrEqual(-180)
        expect(e).toBeLessThanOrEqual(180)
        expect(w > e).toBe(wrap(west) + (east - west) > 180)
      }),
    )
  })

  it('covers every longitude on screen and none off it', () => {
    fc.assert(
      fc.property(viewport, fc.integer({ min: 0, max: 359 }), ({ west, east }, offset) => {
        const bbox = viewportBbox(west, -10, east, 10)
        const lon = wrap(west + offset)
        // ±180 are one meridian: a view ending on it covers it under either name.
        const covered = covers(bbox, lon) || (lon === -180 && covers(bbox, 180))
        expect(covered).toBe(offset <= east - west)
      }),
    )
  })

  it('is the whole world once the view is a world wide', () => {
    expect(viewportBbox(-200, -95, 170, 95)).toEqual([-180, -90, 180, 90])
  })
})

describe('searchBody', () => {
  it('leaves out empty filters, which the API would read as "match nothing"', () => {
    const body = searchBody({ q: '  ', kinds: [], collections: [], area: undefined })
    expect(Object.keys(body)).toEqual(['limit'])
  })
})

describe('nextPage [SRCH-2]', () => {
  it("keeps the query and takes the next link's token", () => {
    const body = searchBody({ q: 'alpha', kinds: ['raster'], collections: [], area: undefined })
    const link = {
      rel: 'next' as const,
      href: '/search',
      type: 'application/geo+json' as const,
      method: 'POST' as const,
      body: { token: 'abc' },
      merge: true as const,
    }
    expect(nextPage(body, [link])).toEqual({ ...body, token: 'abc' })
    expect(nextPage(body, [])).toBeUndefined()
  })
})
