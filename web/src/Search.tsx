import { useEffect, useMemo, useState, type FormEvent } from 'react'
import type { Collection, Kind, StacItem } from './api'
import { MapView } from './MapView'
import { itemMarking } from './markings'
import { searchBody, type Bbox, type Filters } from './search'
import { MAX_RESULTS, useSearch } from './useSearch'

const KINDS: Kind[] = ['raster', 'vector', 'pointcloud', 'mesh', 'tabular', 'document', 'generic']
// Shown first in an item's details; the rest follow alphabetically.
const CORE = ['datetime', 'ingest:kind', 'ingest:media_type', 'file:size', 'proj:code', 'ingest:extraction']

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value]
}

function bytes(size: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const power = Math.min(units.length - 1, Math.floor(Math.log(Math.max(size, 1)) / Math.log(1024)))
  return `${(size / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`
}

function show(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

type FilterProps = {
  collections: Collection[]
  filters: Filters
  onChange: (filters: Filters) => void
  inArea: boolean
  onInArea: (inArea: boolean) => void
}

function FilterPanel({ collections, filters, onChange, inArea, onInArea }: FilterProps) {
  const [text, setText] = useState(filters.q)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onChange({ ...filters, q: text })
  }
  return (
    <section className="panel filters">
      <form onSubmit={submit} className="query">
        <input
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Filename or tag, e.g. ortho 2024"
          aria-label="Search text"
          maxLength={256}
        />
        <button type="submit">Search</button>
      </form>
      <label className="check">
        <input type="checkbox" checked={inArea} onChange={(e) => onInArea(e.target.checked)} />
        Only what's in the map view
      </label>
      <fieldset>
        <legend>Kind</legend>
        {KINDS.map((kind) => (
          <label key={kind} className="check">
            <input
              type="checkbox"
              checked={filters.kinds.includes(kind)}
              onChange={() => onChange({ ...filters, kinds: toggle(filters.kinds, kind) })}
            />
            {kind}
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>Collection</legend>
        {collections.map((c) => (
          <label key={c.id} className="check">
            <input
              type="checkbox"
              checked={filters.collections.includes(c.id)}
              onChange={() => onChange({ ...filters, collections: toggle(filters.collections, c.id) })}
            />
            {c.title}
          </label>
        ))}
      </fieldset>
    </section>
  )
}

function ItemDetails({ item, collections }: { item: StacItem; collections: Collection[] }) {
  const marking = itemMarking(item, collections)
  const props = item.properties as Record<string, unknown>
  const rest = Object.keys(props)
    .filter((k) => !CORE.includes(k) && !['title', 'created', 'keywords'].includes(k) && !k.startsWith('ingest:marking'))
    .toSorted()
  return (
    <section className="panel details">
      <h2>Item</h2>
      <div className="title">{item.properties.title}</div>
      <span className="chip" style={{ background: marking.banner_color }}>
        {marking.label}
      </span>{' '}
      <code>{item.collection}</code>
      {item.properties.keywords.length > 0 && <p className="muted">{item.properties.keywords.join(', ')}</p>}
      <dl>
        {[...CORE, ...rest]
          .filter((k) => props[k] !== undefined && props[k] !== null)
          .map((k) => (
            <div key={k} className="row">
              <dt>{k}</dt>
              <dd>{k === 'file:size' ? bytes(item.properties['file:size']) : show(props[k])}</dd>
            </div>
          ))}
        {item.bbox && (
          <div className="row">
            <dt>bbox</dt>
            <dd>{item.bbox.map((v) => v.toFixed(4)).join(', ')}</dd>
          </div>
        )}
      </dl>
    </section>
  )
}

type ResultsProps = {
  items: StacItem[]
  collections: Collection[]
  selected: string | undefined
  onSelect: (id: string) => void
}

function Results({ items, collections, selected, onSelect }: ResultsProps) {
  return (
    <ol className="results">
      {items.map((item) => {
        const marking = itemMarking(item, collections)
        return (
          <li key={item.id}>
            <button
              className={`result${item.id === selected ? ' selected' : ''}`}
              onClick={() => onSelect(item.id)}
              title={item.geometry ? 'Show on map' : 'No footprint'}
            >
              <span className="title">{item.properties.title}</span>
              <span className="meta">
                <span className="chip" style={{ background: marking.banner_color }}>
                  {marking.label}
                </span>
                {item.properties['ingest:kind']} · {bytes(item.properties['file:size'])} ·{' '}
                {new Date(item.properties.datetime).toLocaleDateString()}
                {!item.geometry && ' · no footprint'}
              </span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

const NO_FILTERS: Filters = { q: '', kinds: [], collections: [], area: undefined }

/** Search the catalog on a map. Reports what it shows, so the page banner covers it (ACC-3). */
export function Search({ collections, onShown }: { collections: Collection[]; onShown: (items: StacItem[]) => void }) {
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [inArea, setInArea] = useState(true)
  const [viewport, setViewport] = useState<Bbox | undefined>(undefined)
  const [selected, setSelected] = useState<string | undefined>(undefined)

  const body = useMemo(() => searchBody({ ...filters, area: inArea ? viewport : undefined }), [filters, inArea, viewport])
  const search = useSearch(body)
  const shown = search.items
  useEffect(() => onShown(shown), [shown, onShown])
  const item = shown.find((i) => i.id === selected)

  return (
    <div className="search">
      <aside className="sidebar">
        <FilterPanel collections={collections} filters={filters} onChange={setFilters} inArea={inArea} onInArea={setInArea} />
        {item && <ItemDetails item={item} collections={collections} />}
        <section className="panel">
          <h2>
            {shown.length} {shown.length === 1 ? 'item' : 'items'}
            {search.loading && ' · searching…'}
          </h2>
          {search.error && <p className="error">Search failed: {search.error}</p>}
          {!search.loading && shown.length === 0 && <p className="muted">Nothing matches. Try zooming out or clearing filters.</p>}
          <Results items={shown} collections={collections} selected={selected} onSelect={setSelected} />
          {search.more && (
            <button onClick={search.loadMore} disabled={search.loading}>
              Load more
            </button>
          )}
          {shown.length >= MAX_RESULTS && <p className="muted">Showing the newest {MAX_RESULTS}; narrow the search to see others.</p>}
        </section>
      </aside>
      <MapView items={shown} selected={selected} onSelect={setSelected} onViewport={setViewport} />
    </div>
  )
}
