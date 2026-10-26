import type { Kind, NextLink, SearchFilters, SearchIn } from './api'

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

/** A calendar day, `YYYY-MM-DD`, as a date input gives it; '' leaves that end open. */
export type Day = string

export type Filters = {
  q: string
  kinds: Kind[] // none ticked means any kind
  collections: string[] // none chosen means every readable collection
  // When the data was captured (or uploaded, if the file doesn't say): first and last day, UTC
  from: Day
  to: Day
  // When it was uploaded: first and last day, UTC
  uploadedFrom: Day
  uploadedTo: Day
}

export const NO_FILTERS: Filters = { q: '', kinds: [], collections: [], from: '', to: '', uploadedFrom: '', uploadedTo: '' }

/** The STAC interval covering the days `from` to `to` inclusive, or undefined for any time. */
export function interval(from: Day, to: Day): string | undefined {
  if (!from && !to) return undefined
  const start = from ? `${from}T00:00:00Z` : '..'
  const end = to ? `${to}T23:59:59.999Z` : '..'
  return `${start}/${end}`
}

/** What `filters` ask the API for, wherever it is asked: a search, or the map's footprints.
 * Empty filters are left out, not sent empty: the API reads an empty list as "match nothing". */
export function filterBody(filters: Filters): SearchFilters {
  const body: SearchFilters = {}
  const q = filters.q.trim()
  if (q) body.q = q
  if (filters.kinds.length) body.kinds = filters.kinds
  if (filters.collections.length) body.collections = filters.collections
  const datetime = interval(filters.from, filters.to)
  if (datetime) body.datetime = datetime
  const created = interval(filters.uploadedFrom, filters.uploadedTo)
  if (created) body.created = created
  return body
}

/** The first page's request for `filters` within `area` (undefined: anywhere, footprint or
 * not). */
export function searchBody(filters: Filters, area: Bbox | undefined): SearchIn {
  const body: SearchIn = { ...filterBody(filters), limit: PAGE_SIZE }
  if (area) body.bbox = area
  return body
}

/** The request for the page after one that ended with `links`, or undefined on the last. */
export function nextPage(body: SearchIn, links: NextLink[]): SearchIn | undefined {
  const next = links.find((link) => link.rel === 'next')
  return next && { ...body, ...next.body }
}
