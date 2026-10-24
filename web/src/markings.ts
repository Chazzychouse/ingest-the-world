import type { Collection, Marking } from './api'

export const UNCLASSIFIED: Marking = { code: 'UNCLASSIFIED', label: 'UNCLASSIFIED', banner_color: '#007a33' }

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
  return top ? { code: top.marking, label: top.marking_label, banner_color: top.marking_color } : UNCLASSIFIED
}
