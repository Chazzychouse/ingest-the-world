import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { area, FADE_TO, pickId } from './footprintSize'

const feature = (id: string, size: number) => ({ properties: { id, area: size } })

describe('area', () => {
  it('measures a bbox across the antimeridian the short way', () => {
    expect(area([179, 0, -179, 1])).toBe(2)
    expect(area([-180, -90, 180, 90])).toBe(64800)
  })
})

describe('pickId', () => {
  it('picks the smallest footprint, whatever order they come in', () => {
    fc.assert(
      fc.property(fc.uniqueArray(fc.double({ min: 0.001, max: 100, noNaN: true }), { minLength: 1, maxLength: 8 }), (sizes) => {
        const features = sizes.map((size, i) => feature(`item-${i}`, size))
        const smallest = sizes.indexOf(Math.min(...sizes))
        expect(pickId(features, 1000)).toBe(`item-${smallest}`)
      }),
    )
  })

  it("passes over a footprint too large to be filled, so a click inside it isn't a click on it", () => {
    expect(pickId([feature('world', 64800)], 60000)).toBeUndefined()
    expect(pickId([feature('world', 64800), feature('town', 0.01)], 60000)).toBe('town')
    expect(pickId([feature('region', FADE_TO * 1000)], 1000)).toBe('region')
  })

  it('ignores features without an id or an area', () => {
    expect(pickId([{ properties: { id: 'x' } }, { properties: { area: 1 } }], 1000)).toBeUndefined()
  })
})
