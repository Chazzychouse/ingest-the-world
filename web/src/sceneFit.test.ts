import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { fit } from './sceneFit'

const coordinate = fc.double({ min: -1e7, max: 1e7, noNaN: true })

describe('fit', () => {
  it('stands outside the sphere, sees all of it, and clips none of it', () => {
    fc.assert(
      fc.property(fc.tuple(coordinate, coordinate, coordinate), fc.double({ min: 0.01, max: 1e6, noNaN: true }), fc.integer({ min: 20, max: 90 }), (center, radius, fov) => {
        const placed = fit({ center, radius }, fov)
        const [px, py, pz] = placed.position
        const distance = Math.hypot(px - center[0], py - center[1], pz - center[2])
        // The sphere's angular radius from the camera fits inside half the field of view.
        expect(Math.asin(Math.min(1, radius / distance))).toBeLessThanOrEqual((fov * Math.PI) / 360)
        expect(placed.near).toBeLessThan(distance - radius)
        expect(placed.far).toBeGreaterThan(distance + radius)
      }),
    )
  })
})
