import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useApi, type Collection, type StacItem } from './api'
import { bytes } from './format'
import { MapView } from './MapView'
import { itemMarking } from './markings'
import { KINDS, MAX_QUERY_LENGTH } from './schema'
import { NO_FILTERS, searchBody, type Bbox, type Filters } from './search'
import { fromQuery, toQuery, type View } from './searchUrl'
import { MAX_RESULTS, useSearch } from './useSearch'

// Shown first in an item's details; the rest follow alphabetically.
const CORE = ['datetime', 'ingest:kind', 'ingest:media_type', 'file:size', 'proj:code', 'ingest:extraction']
// Shown elsewhere in the details, or only meaningful to the banner.
const HIDDEN = ['title', 'created', 'keywords', 'ingest:marking', 'ingest:marking_rank']
// How long typing pauses before the text is searched.
const TYPING_MS = 300

function toggle<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((v) => v !== value) : [...values, value]
}

function when(datetime: string): string {
  return new Date(datetime).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function show(key: string, value: unknown): string {
  if (key === 'file:size' && typeof value === 'number') return bytes(value)
  if (key === 'datetime' && typeof value === 'string') return when(value)
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

/** How many filters besides the text and the map view narrow the search. */
function narrowing({ kinds, collections, from, to }: Filters): number {
  return kinds.length + collections.length + (from || to ? 1 : 0)
}

function titleOf(collections: Collection[], id: string): string {
  return collections.find((c) => c.id === id)?.title ?? id
}

/** The search box: searches as you type, once typing pauses, or at once on Enter. */
function QueryBox({ q, onSearch }: { q: string; onSearch: (q: string) => void }) {
  const [text, setText] = useState(q)
  const latest = useRef(onSearch)
  useEffect(() => {
    latest.current = onSearch
  })
  useEffect(() => {
    if (text === q) return
    const timer = setTimeout(() => latest.current(text), TYPING_MS)
    return () => clearTimeout(timer)
  }, [text, q])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onSearch(text)
  }
  return (
    <form onSubmit={submit} className="query" role="search">
      <input
        type="search"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Search filenames and tags"
        aria-label="Search filenames and tags"
        maxLength={MAX_QUERY_LENGTH}
      />
    </form>
  )
}

type FilterProps = {
  collections: Collection[]
  filters: Filters
  onChange: (filters: Filters) => void
}

function FilterPanel({ collections, filters, onChange }: FilterProps) {
  return (
    <div className="filter-body">
      <fieldset className="dates">
        <legend>Date, UTC</legend>
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
        <div className="toggles">
          {KINDS.map((kind) => (
            <button
              key={kind}
              type="button"
              className="toggle"
              aria-pressed={filters.kinds.includes(kind)}
              onClick={() => onChange({ ...filters, kinds: toggle(filters.kinds, kind) })}
            >
              {kind}
            </button>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>Collection</legend>
        <div className="options">
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
        </div>
      </fieldset>
    </div>
  )
}

type DetailsProps = {
  item: StacItem
  collections: Collection[]
  onZoom: (bbox: Bbox) => void
  onBack: () => void
}

function ItemDetails({ item, collections, onZoom, onBack }: DetailsProps) {
  const [copied, setCopied] = useState(false)
  const marking = itemMarking(item, collections)
  const bbox = item.bbox
  const props = item.properties as Record<string, unknown>
  const rest = Object.keys(props)
    .filter((k) => !CORE.includes(k) && !HIDDEN.includes(k))
    .toSorted()
  const copy = () => {
    navigator.clipboard.writeText(window.location.href).then(
      () => setCopied(true),
      () => setCopied(false),
    )
  }
  return (
    <section className="details" aria-label="Item details">
      <button className="link back" onClick={onBack}>
        ← Results
      </button>
      <h2 className="title">{item.properties.title}</h2>
      <div className="meta">
        <span className="chip" style={{ background: marking.banner_color }}>
          {marking.label}
        </span>
        {titleOf(collections, item.collection)}
      </div>
      {item.properties.keywords.length > 0 && (
        <ul className="tags">
          {item.properties.keywords.map((k) => (
            <li key={k}>{k}</li>
          ))}
        </ul>
      )}
      <div className="actions">
        {bbox ? (
          <button className="secondary" onClick={() => onZoom(bbox)}>
            Zoom to footprint
          </button>
        ) : (
          <span className="muted">No footprint to show on the map</span>
        )}
        <button className="secondary" onClick={copy}>
          {copied ? 'Link copied' : 'Copy link'}
        </button>
      </div>
      <dl>
        {[...CORE, ...rest]
          .filter((k) => props[k] !== undefined && props[k] !== null)
          .map((k) => (
            <div key={k} className="row">
              <dt>{k}</dt>
              <dd>{show(k, props[k])}</dd>
            </div>
          ))}
        {bbox && (
          <div className="row">
            <dt>bbox</dt>
            <dd>{bbox.map((v) => v.toFixed(4)).join(', ')}</dd>
          </div>
        )}
        <div className="row">
          <dt>id</dt>
          <dd>{item.id}</dd>
        </div>
      </dl>
    </section>
  )
}

type ResultsProps = {
  items: StacItem[]
  collections: Collection[]
  selected: string | undefined
  hovered: string | undefined
  onSelect: (id: string) => void
  onHover: (id: string | undefined) => void
}

function Results({ items, collections, selected, hovered, onSelect, onHover }: ResultsProps) {
  return (
    <ol className="results" onMouseLeave={() => onHover(undefined)}>
      {items.map((item) => {
        const marking = itemMarking(item, collections)
        const state = item.id === selected ? ' selected' : item.id === hovered ? ' hovered' : ''
        return (
          <li key={item.id}>
            <button
              className={`result${state}`}
              onClick={() => onSelect(item.id)}
              onMouseEnter={() => onHover(item.id)}
              onFocus={() => onHover(item.id)}
            >
              <span className="title">{item.properties.title}</span>
              <span className="meta">
                <span className="chip" style={{ background: marking.banner_color }}>
                  {marking.label}
                </span>
                {item.properties['ingest:kind']} · {bytes(item.properties['file:size'])} ·{' '}
                {new Date(item.properties.datetime).toLocaleDateString()}
              </span>
              <span className="meta">
                {titleOf(collections, item.collection)}
                {!item.geometry && ' · no footprint'}
              </span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

type EmptyProps = { inArea: boolean; filtered: boolean; onAnywhere: () => void; onClear: () => void }

/** Why nothing matched, and the one change most likely to find something. */
function Empty({ inArea, filtered, onAnywhere, onClear }: EmptyProps) {
  return (
    <div className="empty">
      <p>Nothing matches{inArea ? ' in this part of the map' : ''}.</p>
      {inArea && (
        <button className="secondary" onClick={onAnywhere}>
          Search anywhere
        </button>
      )}
      {!inArea && filtered && (
        <button className="secondary" onClick={onClear}>
          Clear filters
        </button>
      )}
    </div>
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
  const [hovered, setHovered] = useState<string | undefined>(undefined)
  const [fit, setFit] = useState<{ bbox: Bbox } | undefined>(undefined)
  const [filtersOpen, setFiltersOpen] = useState(narrowing(initial.filters) > 0)
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
  const count = narrowing(filters)
  const clear = () => setFilters({ ...NO_FILTERS, q: filters.q })
  const n = search.items.length

  return (
    <div className="search">
      <aside className="sidebar">
        <div className="controls">
          <QueryBox q={filters.q} onSearch={(q) => setFilters({ ...filters, q })} />
          <div className="bar">
            <label className="check">
              <input type="checkbox" checked={inArea} onChange={(e) => setInArea(e.target.checked)} />
              Only what's in the map view
            </label>
            <button
              className="link"
              aria-expanded={filtersOpen}
              aria-controls="search-filters"
              onClick={() => setFiltersOpen(!filtersOpen)}
            >
              Filters{count > 0 && <span className="count">{count}</span>}
            </button>
          </div>
          {filtersOpen && (
            <div id="search-filters">
              <FilterPanel collections={collections} filters={filters} onChange={setFilters} />
              {count > 0 && (
                <button className="link" onClick={clear}>
                  Clear filters
                </button>
              )}
            </div>
          )}
        </div>
        {item && (
          <ItemDetails
            key={item.id}
            item={item}
            collections={collections}
            onZoom={(bbox) => setFit({ bbox })}
            onBack={() => setSelected(undefined)}
          />
        )}
        {selected && !item && (
          <section className="details">
            <button className="link back" onClick={() => setSelected(undefined)}>
              ← Results
            </button>
            {selection.error ? (
              <p className="error">
                {selection.error.status === 404
                  ? "That item doesn't exist, or isn't shared with you."
                  : `Couldn't load the item (${selection.error.message}). Try again shortly.`}
              </p>
            ) : (
              <p className="muted">Loading the item…</p>
            )}
          </section>
        )}
        <section className="listing" hidden={selected !== undefined} aria-live="polite" aria-busy={search.loading}>
          <p className="summary">
            {search.loading && n === 0 ? 'Searching…' : `${n} ${n === 1 ? 'item' : 'items'}`}
            {inArea && ' in the map view'}
            {search.loading && n > 0 && <span className="muted"> · updating</span>}
          </p>
          {inArea && <p className="hint">Items without a footprint appear only when you search anywhere.</p>}
          {search.error && <p className="error">The search failed ({search.error}). Change a filter to try again.</p>}
          {!search.loading && !search.error && n === 0 && (
            <Empty inArea={inArea} filtered={count > 0 || filters.q !== ''} onAnywhere={() => setInArea(false)} onClear={clear} />
          )}
          <Results
            items={search.items}
            collections={collections}
            selected={selected}
            hovered={hovered}
            onSelect={select}
            onHover={setHovered}
          />
          {search.more && (
            <button className="secondary more" onClick={search.loadMore} disabled={search.loading}>
              {search.loading ? 'Loading…' : 'Load more'}
            </button>
          )}
          {n >= MAX_RESULTS && <p className="hint">Showing the newest {MAX_RESULTS}. Narrow the search to see others.</p>}
        </section>
      </aside>
      <MapView
        items={shown}
        selected={selected}
        hovered={hovered}
        onSelect={select}
        onHover={setHovered}
        onViewport={onViewport}
        initialView={initial.view}
        fit={fit}
      />
    </div>
  )
}
