import type { Kind } from './api'

// One line drawing per kind, on a 24-unit grid, stroked in the current colour.
const PATHS: Record<Kind, string> = {
  raster: 'M4 5h16v14H4z M4 16l5-5 4 4 2-2 5 5 M15.5 9.5a1.5 1.5 0 1 0 0-.01',
  vector: 'M4 18l5-9 6 5 5-9 M4 18h.01 M9 9h.01 M15 14h.01 M20 5h.01',
  pointcloud:
    'M6 8h.01 M10 6h.01 M14 7h.01 M18 9h.01 M5 13h.01 M9 11h.01 M13 12h.01 M17 13h.01 M7 17h.01 M11 16h.01 M15 17h.01 M19 16h.01',
  mesh: 'M12 3l8 4.5v9L12 21l-8-4.5v-9z M12 12l8-4.5 M12 12v9 M12 12L4 7.5',
  tabular: 'M4 5h16v14H4z M4 10h16 M4 15h16 M10 5v14',
  document: 'M6 3h8l4 4v14H6z M14 3v4h4 M9 12h6 M9 16h6',
  generic: 'M6 3h8l4 4v14H6z M14 3v4h4',
}

/** A kind's drawing, in a thumbnail's place when an item has none. */
export function KindIcon({ kind, className }: { kind: Kind; className: string }) {
  return (
    <span className={`${className} kind`} title={kind} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d={PATHS[kind]} />
      </svg>
    </span>
  )
}
