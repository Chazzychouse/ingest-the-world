import type { Kind } from './api'
import { COLLECTION_ID, KINDS, MAX_FILTER_VALUES, MAX_QUERY_LENGTH } from './schema'
import { NO_FILTERS, type Day, type Filters } from './search'

/** Where the map looks: its centre and zoom, as MapLibre takes them. */
export type View = { lon: number; lat: number; zoom: number }

/** Everything a search link restores. */
export type SearchUrl = {
  filters: Filters
  inArea: boolean // only what's in the map view
  item: string | undefined // the item shown in detail
  view: View | undefined // undefined: the map's default
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const DAY = /^\d{4}-\d{2}-\d{2}$/
// MapLibre's zoom range; below 0 the world is smaller than the map, as on a phone.
const [MIN_ZOOM, MAX_ZOOM] = [-2, 24]

const round = (value: number, places: number) => Number(value.toFixed(places))

/** The view rounded as a link writes it: about a metre at the equator, and a tenth of a zoom. */
export function rounded({ lon, lat, zoom }: View): View {
  return { lon: round(lon, 5), lat: round(lat, 5), zoom: round(zoom, 1) }
}

/** The query string for `state`, leaving out whatever is at its default. */
export function toQuery(state: SearchUrl): string {
  const params = new URLSearchParams()
  const { q, kinds, collections, from, to, uploadedFrom, uploadedTo } = state.filters
  if (q) params.set('q', q)
  for (const kind of kinds) params.append('kind', kind)
  for (const id of collections) params.append('collection', id)
  if (from) params.set('from', from)
  if (to) params.set('to', to)
  if (uploadedFrom) params.set('uploaded-from', uploadedFrom)
  if (uploadedTo) params.set('uploaded-to', uploadedTo)
  if (!state.inArea) params.set('anywhere', '1')
  if (state.item) params.set('item', state.item)
  if (state.view) {
    const { lon, lat, zoom } = rounded(state.view)
    params.set('map', `${zoom}/${lat}/${lon}`)
  }
  return params.toString()
}

function day(text: string | null): Day {
  return text && DAY.test(text) && !Number.isNaN(Date.parse(text)) ? text : ''
}

/** A range of days from a link: either end may be open; one ending before it starts is dropped. */
function days(first: string | null, last: string | null): [Day, Day] {
  const [from, to] = [day(first), day(last)]
  return from && to && from > to ? ['', ''] : [from, to]
}

/** The distinct values of `values` that `keep` accepts, at most as many as the API takes. */
function distinct<T extends string>(values: string[], keep: (value: string) => value is T): T[] {
  return [...new Set(values.filter(keep))].slice(0, MAX_FILTER_VALUES)
}

function view(text: string | null): View | undefined {
  const [zoom, lat, lon] = (text ?? '').split('/').map(Number)
  if (zoom === undefined || lat === undefined || lon === undefined) return undefined
  if (!(zoom >= MIN_ZOOM && zoom <= MAX_ZOOM && Math.abs(lat) <= 90 && Math.abs(lon) <= 180)) return undefined
  return { lon, lat, zoom }
}

/**
 * The state a query string describes. A link can be hand-edited, so anything the API would
 * reject is dropped rather than sent: NULs, unknown kinds, malformed ids, a range ending before
 * it starts.
 */
export function fromQuery(query: string): SearchUrl {
  const params = new URLSearchParams(query)
  const kinds = distinct(params.getAll('kind'), (k): k is Kind => (KINDS as string[]).includes(k))
  const collections = distinct(params.getAll('collection'), (c): c is string => COLLECTION_ID.test(c))
  const [from, to] = days(params.get('from'), params.get('to'))
  const [uploadedFrom, uploadedTo] = days(params.get('uploaded-from'), params.get('uploaded-to'))
  const item = (params.get('item') ?? '').toLowerCase()
  return {
    filters: { ...NO_FILTERS, q: (params.get('q') ?? '').replaceAll('\0', '').slice(0, MAX_QUERY_LENGTH), kinds, collections, from, to, uploadedFrom, uploadedTo },
    inArea: params.get('anywhere') !== '1',
    item: UUID.test(item) ? item : undefined,
    view: view(params.get('map')),
  }
}
