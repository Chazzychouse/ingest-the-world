// A search link reopens the search it was copied from, and a hand-edited one never sends the
// API a filter it would reject.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { COLLECTION_ID, KINDS, MAX_FILTER_VALUES, MAX_QUERY_LENGTH } from './schema'
import { fromQuery, toQuery, type SearchUrl } from './searchUrl'

const day = fc
  .date({ min: new Date('1900-01-01T00:00:00Z'), max: new Date('2199-12-31T00:00:00Z'), noInvalidDate: true })
  .map((d) => d.toISOString().slice(0, 10))

const range = fc
  .tuple(fc.option(day, { nil: '' }), fc.option(day, { nil: '' }))
  .map(([a, b]) => (a && b && a > b ? [b, a] : [a, b]))

// Views as a link writes them: rounded to 5 places, zoom to 1.
const mapView = fc.record({
  lon: fc.integer({ min: -18_000_000, max: 18_000_000 }).map((n) => n / 1e5),
  lat: fc.integer({ min: -9_000_000, max: 9_000_000 }).map((n) => n / 1e5),
  zoom: fc.integer({ min: 0, max: 240 }).map((n) => n / 10),
})

const state: fc.Arbitrary<SearchUrl> = fc
  .record({
    q: fc.string({ unit: 'grapheme', maxLength: 40 }).filter((q) => !q.includes('\0')),
    kinds: fc.uniqueArray(fc.constantFrom(...KINDS)),
    collections: fc.uniqueArray(fc.stringMatching(COLLECTION_ID), { maxLength: 5 }),
    range,
    inArea: fc.boolean(),
    item: fc.option(fc.uuid(), { nil: undefined }),
    view: fc.option(mapView, { nil: undefined }),
  })
  .map(({ q, kinds, collections, range: [from = '', to = ''], inArea, item, view }) => ({
    filters: { q, kinds, collections, from, to },
    inArea,
    item,
    view,
  }))

// Query strings someone might paste or edit: real parameter names with arbitrary values.
const query = fc
  .array(
    fc.tuple(
      fc.constantFrom('q', 'kind', 'collection', 'from', 'to', 'anywhere', 'item', 'map', 'other'),
      fc.oneof(fc.string(), fc.constantFrom(...KINDS), day, fc.uuid(), fc.constant('1/2/3'), fc.constant('a\0b')),
    ),
    { maxLength: 150 },
  )
  .map((pairs) => new URLSearchParams(pairs).toString())

describe('search links', () => {
  it('round-trip every search', () => {
    fc.assert(
      fc.property(state, (s) => {
        expect(fromQuery(toQuery(s))).toEqual(s)
      }),
    )
  })

  it('are empty for the default search', () => {
    expect(toQuery(fromQuery(''))).toBe('')
  })

  it('read any query as a search the API accepts', () => {
    fc.assert(
      fc.property(fc.oneof(query, fc.string()), (text) => {
        const { filters, item, view } = fromQuery(text)
        expect(filters.q.length).toBeLessThanOrEqual(MAX_QUERY_LENGTH)
        expect(filters.q).not.toContain('\0')
        for (const list of [filters.kinds, filters.collections]) {
          expect(new Set(list).size).toBe(list.length)
          expect(list.length).toBeLessThanOrEqual(MAX_FILTER_VALUES)
        }
        expect(filters.kinds.every((k) => KINDS.includes(k))).toBe(true)
        expect(filters.collections.every((c) => COLLECTION_ID.test(c))).toBe(true)
        for (const d of [filters.from, filters.to]) expect(d === '' || !Number.isNaN(Date.parse(d))).toBe(true)
        expect(filters.from && filters.to ? filters.from <= filters.to : true).toBe(true)
        expect(item === undefined || /^[0-9a-f-]{36}$/.test(item)).toBe(true)
        if (view) expect(Math.abs(view.lat) <= 90 && Math.abs(view.lon) <= 180).toBe(true)
      }),
    )
  })

  it('read the same search back from a link to what they read', () => {
    fc.assert(
      fc.property(query, (text) => {
        const once = fromQuery(text)
        expect(fromQuery(toQuery(once))).toEqual(fromQuery(toQuery(fromQuery(toQuery(once)))))
      }),
    )
  })
})
