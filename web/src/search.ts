import type { Kind, NextLink, SearchIn } from './api'

/** `[west, south, east, north]` as STAC writes it; west > east crosses the antimeridian. */
export type Bbox = [number, number, number, number]

export const WORLD: Bbox = [-180, -90, 180, 90]
export const PAGE_SIZE = 50

/** A longitude in [-180, 180). */
function wrapWest(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180
}

/** A longitude in (-180, 180], so an east edge on the antimeridian stays at 180. */
function wrapEast(lon: number): number {
  return -wrapWest(-lon)
}

const clampLat = (lat: number) => Math.min(90, Math.max(-90, lat))

/**
 * The search bbox for a map viewport. Once the world has wrapped, MapLibre reports longitudes
 * past ±180; the API takes them in [-180, 180], with west > east for a view across the
 * antimeridian. A view a world or more wide is the whole world.
 */
export function viewportBbox(west: number, south: number, east: number, north: number): Bbox {
  const [s, n] = [clampLat(south), clampLat(north)]
  if (east - west >= 360) return [-180, s, 180, n]
  return [wrapWest(west), s, wrapEast(east), n]
}

export type Filters = {
  q: string
  kinds: Kind[] // none ticked means any kind
  collections: string[] // none chosen means every readable collection
  area: Bbox | undefined // undefined: anywhere, footprint or not
}

/** The first page's request for `filters`. Empty filters are left out, not sent empty: the
 * API reads an empty list as "match nothing". */
export function searchBody(filters: Filters): SearchIn {
  const body: SearchIn = { limit: PAGE_SIZE }
  const q = filters.q.trim()
  if (q) body.q = q
  if (filters.kinds.length) body.kinds = filters.kinds
  if (filters.collections.length) body.collections = filters.collections
  if (filters.area) body.bbox = filters.area
  return body
}

/** The request for the page after one that ended with `links`, or undefined on the last. */
export function nextPage(body: SearchIn, links: NextLink[]): SearchIn | undefined {
  const next = links.find((link) => link.rel === 'next')
  return next && { ...body, ...next.body }
}
