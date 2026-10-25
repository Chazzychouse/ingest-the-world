// SRCH-1 starts in the browser: the bbox sent for a viewport must cover exactly what's on screen.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { interval, nextPage, NO_FILTERS, searchBody, viewportBbox, type Bbox } from './search'

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
    const body = searchBody({ ...NO_FILTERS, q: '  ' }, undefined)
    expect(Object.keys(body)).toEqual(['limit'])
  })
})

// Days in years a date input writes with four digits.
const day = fc
  .date({ min: new Date('1900-01-01T00:00:00Z'), max: new Date('2199-12-31T00:00:00Z'), noInvalidDate: true })
  .map((d) => d.toISOString().slice(0, 10))

describe('interval [SRCH-1]', () => {
  it('matches exactly the instants on the days chosen, either end open', () => {
    const instant = fc.date({ min: new Date('1899-12-31T00:00:00Z'), max: new Date('2200-01-01T23:59:59Z'), noInvalidDate: true })
    const end = fc.option(day, { nil: '' })
    fc.assert(
      fc.property(end, end, instant, (from, to, at) => {
        fc.pre(!(from && to && from > to))
        const text = interval(from, to)
        if (!from && !to) {
          expect(text).toBeUndefined()
          return
        }
        const [start, stop] = (text ?? '').split('/').map((t) => (t === '..' ? undefined : Date.parse(t)))
        const inside = (start === undefined || start <= at.getTime()) && (stop === undefined || at.getTime() <= stop)
        const onDays = (!from || from <= at.toISOString().slice(0, 10)) && (!to || at.toISOString().slice(0, 10) <= to)
        expect(inside).toBe(onDays)
      }),
    )
  })
})

describe('nextPage [SRCH-2]', () => {
  it("keeps the query and takes the next link's token", () => {
    const body = searchBody({ ...NO_FILTERS, q: 'alpha', kinds: ['raster'] }, undefined)
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
