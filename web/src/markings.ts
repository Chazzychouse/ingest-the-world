import type { Collection, Marking } from './api'

// The rank-0 baseline (migration 0001), shown before any data has loaded.
export const UNCLASSIFIED: Marking = { code: 'UNCLASSIFIED', label: 'UNCLASSIFIED', banner_color: '#007a33', rank: 0 }

/**
 * The banner for a screen showing `collections`: the highest-ranked marking among them
 * (ACC-3). Ranks come from the API, so new markings need no change here, and the result
 * doesn't depend on the order the collections arrive in.
 */
export function bannerFor(collections: Collection[]): Marking {
  let top: Collection | undefined
  for (const c of collections) {
    if (!top || c.marking_rank > top.marking_rank) top = c
  }
  if (!top) return UNCLASSIFIED
  return { code: top.marking, label: top.marking_label, banner_color: top.marking_color, rank: top.marking_rank }
}
