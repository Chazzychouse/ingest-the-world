import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useApi, type Collection, type StacItem } from './api'
import { MapView } from './MapView'
import { itemMarking } from './markings'
import { KINDS, MAX_QUERY_LENGTH } from './schema'
import { searchBody, type Bbox, type Filters } from './search'
import { fromQuery, toQuery, type View } from './searchUrl'
import { MAX_RESULTS, useSearch } from './useSearch'

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
          maxLength={MAX_QUERY_LENGTH}
        />
        <button type="submit">Search</button>
      </form>
      <label className="check">
        <input type="checkbox" checked={inArea} onChange={(e) => onInArea(e.target.checked)} />
        Only what's in the map view
      </label>
      <fieldset className="dates">
        <legend>Date (UTC)</legend>
        <label>
          From
          <input
            type="date"
            value={filters.from}
            max={filters.to || undefined}
            onChange={(e) => onChange({ ...filters, from: e.target.value })}
          />
        </label>
        <label>
          To
          <input
            type="date"
            value={filters.to}
            min={filters.from || undefined}
            onChange={(e) => onChange({ ...filters, to: e.target.value })}
          />
        </label>
      </fieldset>
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

type DetailsProps = {
  item: StacItem
  collections: Collection[]
  onZoom: (bbox: Bbox) => void
  onClose: () => void
}

function ItemDetails({ item, collections, onZoom, onClose }: DetailsProps) {
  const [copied, setCopied] = useState(false)
  const marking = itemMarking(item, collections)
  const bbox = item.bbox
  const copy = () => {
    navigator.clipboard.writeText(window.location.href).then(
      () => setCopied(true),
      () => setCopied(false),
    )
  }
  const props = item.properties as Record<string, unknown>
  const rest = Object.keys(props)
    .filter((k) => !CORE.includes(k) && !['title', 'created', 'keywords'].includes(k) && !k.startsWith('ingest:marking'))
    .toSorted()
  return (
    <section className="panel details">
      <h2>Item</h2>
      <div className="title">{item.properties.title}</div>
      <div className="actions">
        {bbox && (
          <button className="link" onClick={() => onZoom(bbox)}>
            Zoom to
          </button>
        )}
        <button className="link" onClick={copy}>
          {copied ? 'Link copied' : 'Copy link'}
        </button>
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
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

/** The item shown in detail: from the results when it's among them, else fetched by id. */
function useSelectedItem(id: string | undefined, shown: StacItem[]) {
  const listed = shown.find((i) => i.id === id)
  const fetched = useApi<StacItem>(id && !listed ? `/items/${id}` : undefined)
  return { item: listed ?? fetched.data, listed: listed !== undefined, error: fetched.error }
}

/** Search the catalog on a map, its state kept in the page's URL so a link reopens it. Reports
 * what it shows, so the page banner covers it (ACC-3). */
export function Search({ collections, onShown }: { collections: Collection[]; onShown: (items: StacItem[]) => void }) {
  const [initial] = useState(() => fromQuery(window.location.search))
  const [filters, setFilters] = useState<Filters>(initial.filters)
  const [inArea, setInArea] = useState(initial.inArea)
  const [viewport, setViewport] = useState<Bbox | undefined>(undefined)
  const [view, setView] = useState<View | undefined>(initial.view)
  const [selected, setSelected] = useState<string | undefined>(initial.item)
  const [fit, setFit] = useState<{ bbox: Bbox } | undefined>(undefined)
  // A link to an item without a map view shows the item once it loads.
  const fitOnLoad = useRef(initial.item !== undefined && initial.view === undefined)

  // Wait for the map's first viewport before searching only what's in it.
  const area = inArea ? viewport : undefined
  const body = useMemo(() => searchBody(filters, area), [filters, area])
  const search = useSearch(body, { enabled: !inArea || viewport !== undefined })
  const selection = useSelectedItem(selected, search.items)
  const item = selection.item

  // An item opened from a link may be outside the results; it's on screen all the same.
  const shown = useMemo(
    () => (item && !selection.listed ? [...search.items, item] : search.items),
    [search.items, item, selection.listed],
  )
  useEffect(() => onShown(shown), [shown, onShown])

  useEffect(() => {
    const query = toQuery({ filters, inArea, item: selected, view })
    window.history.replaceState(window.history.state, '', query ? `?${query}` : window.location.pathname)
  }, [filters, inArea, selected, view])

  useEffect(() => {
    const bbox = item?.bbox
    if (!fitOnLoad.current || !bbox) return
    fitOnLoad.current = false
    setFit({ bbox })
  }, [item])

  const select = (id: string) => {
    setSelected(id)
    const bbox = shown.find((i) => i.id === id)?.bbox
    if (bbox) setFit({ bbox })
  }
  const onViewport = (bbox: Bbox, next: View) => {
    setViewport(bbox)
    setView(next)
  }

  return (
    <div className="search">
      <aside className="sidebar">
        <FilterPanel collections={collections} filters={filters} onChange={setFilters} inArea={inArea} onInArea={setInArea} />
        {item && (
          <ItemDetails
            key={item.id}
            item={item}
            collections={collections}
            onZoom={(bbox) => setFit({ bbox })}
            onClose={() => setSelected(undefined)}
          />
        )}
        {selection.error && (
          <section className="panel">
            <p className="error">
              {selection.error.status === 404
                ? "That item doesn't exist or isn't shared with you."
                : `Couldn't load the item: ${selection.error.message}`}
            </p>
          </section>
        )}
        <section className="panel">
          <h2>
            {search.items.length} {search.items.length === 1 ? 'item' : 'items'}
            {search.loading && ' · searching…'}
          </h2>
          {search.error && <p className="error">Search failed: {search.error}</p>}
          {!search.loading && search.items.length === 0 && (
            <p className="muted">Nothing matches. Try zooming out or clearing filters.</p>
          )}
          <Results items={search.items} collections={collections} selected={selected} onSelect={select} />
          {search.more && (
            <button onClick={search.loadMore} disabled={search.loading}>
              Load more
            </button>
          )}
          {search.items.length >= MAX_RESULTS && (
            <p className="muted">Showing the newest {MAX_RESULTS}; narrow the search to see others.</p>
          )}
        </section>
      </aside>
      <MapView
        items={shown}
        selected={selected}
        onSelect={select}
        onViewport={onViewport}
        initialView={initial.view}
        fit={fit}
      />
    </div>
  )
}
