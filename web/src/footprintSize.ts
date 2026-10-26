import type { ExpressionSpecification } from 'maplibre-gl'
import type { Bbox } from './search'

// A footprint's fill fades out as it grows from this share of the view's area to the next, so a
// handful of world-sized datasets outline the map instead of tinting all of it. Past the second
// it is no longer filled, and a pointer inside it no longer picks it: it picks what is there.
export const FADE_FROM = 0.15
export const FADE_TO = 0.6
// A footprint smaller than this share of the view, a few pixels across, is also marked by a dot
// at its centre, so it can be seen and clicked.
export const DOT_SHARE = 1e-4

/** A bbox's area in square degrees, across the antimeridian too. Tiles carry the same number for
 * every footprint, as `area`. */
export function area([west, south, east, north]: Bbox): number {
  return ((east - west + 360) % 360 || 360) * (north - south)
}

/** A fill's opacity for each footprint, by its `area` against the view's: `opacity` while it is
 * small beside the view, fading to none as it grows to fill it. A footprint without an area is
 * taken as small: an expression that fails draws its layer fully opaque. */
export function sizeFade(viewArea: number, opacity: number): ExpressionSpecification {
  const size: ExpressionSpecification = ['coalesce', ['get', 'area'], 0]
  return ['interpolate', ['linear'], size, FADE_FROM * viewArea, opacity, FADE_TO * viewArea, 0]
}

/** A dot's opacity: shown for a footprint too small beside the view to see, else hidden. */
export function dotOpacity(viewArea: number): ExpressionSpecification {
  return ['case', ['<', ['coalesce', ['get', 'area'], 0], DOT_SHARE * viewArea], 1, 0]
}

type Picked = { properties: Record<string, unknown> }

/** The id of the smallest footprint under the pointer that is still filled, so a small item over a
 * large one is the one picked whichever draws on top. */
export function pickId(features: Picked[], viewArea: number): string | undefined {
  let best: { id: string; size: number } | undefined
  for (const { properties } of features) {
    const { id, area: size } = properties
    if (typeof id !== 'string' || typeof size !== 'number' || size > FADE_TO * viewArea) continue
    if (!best || size < best.size) best = { id, size }
  }
  return best?.id
}
