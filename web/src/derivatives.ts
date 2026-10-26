import type { DeriveReason, StacItem } from './api'

// What each derivative is called in the details; another is named by its role.
const NAMES: Record<string, string> = { cog: 'map preview', thumbnail: 'thumbnail' }

const WHY: Record<DeriveReason, string> = {
  'too-large': 'the file is too large to convert',
  unrenderable: "the file couldn't be drawn",
  'retries-exhausted': 'making it kept failing',
  'dead-lettered': 'making it kept failing',
}

/** What an item's details should say of its derivatives: that they're still being made, and
 * which it lacks because they failed (ING-13). Nothing once all are made. */
export function derivativeNotes(item: StacItem): string[] {
  const { 'ingest:derivatives': state, 'ingest:derivative_failures': failures } = item.properties
  const failed = Object.entries(failures)
    .map(([role, why]) => ({ name: NAMES[role] ?? role, why }))
    .toSorted((a, b) => a.name.localeCompare(b.name))
    .map(({ name, why }) => `No ${name}: ${WHY[why]}.`)
  return state === 'deriving' ? ['Previews and other files are still being made.', ...failed] : failed
}
