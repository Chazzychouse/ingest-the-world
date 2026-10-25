import type { Collection, Marking, StacItem } from './api'

// The rank-0 baseline (migration 0001), shown before any data has loaded.
export const UNCLASSIFIED: Marking = { code: 'UNCLASSIFIED', label: 'UNCLASSIFIED', banner_color: '#007a33', rank: 0 }
// For a marking no loaded collection carries; its rank, which decides the banner, is still the API's.
const UNLISTED_COLOR = '#444444'

export function collectionMarking(c: Collection): Marking {
  return { code: c.marking, label: c.marking_label, banner_color: c.marking_color, rank: c.marking_rank }
}

/** An item's marking. Items carry their marking's code and rank; its label and colour come from
 * a collection with the same marking, since an item can be marked above its own (ACC-3). */
export function itemMarking(item: StacItem, collections: Collection[]): Marking {
  const code = item.properties['ingest:marking']
  const listed = collections.find((c) => c.marking === code)
  if (listed) return collectionMarking(listed)
  return { code, label: code, banner_color: UNLISTED_COLOR, rank: item.properties['ingest:marking_rank'] }
}

/**
 * The banner for a screen showing `markings`: the highest-ranked among them (ACC-3). Ranks come
 * from the API, so new markings need no change here, and the result doesn't depend on the order
 * the data arrives in.
 */
export function highest(markings: Marking[]): Marking {
  let top: Marking | undefined
  for (const m of markings) {
    if (!top || m.rank > top.rank) top = m
  }
  return top ?? UNCLASSIFIED
}

export function bannerFor(collections: Collection[], items: StacItem[] = []): Marking {
  return highest([...collections.map(collectionMarking), ...items.map((i) => itemMarking(i, collections))])
}

/** A collection's full marking as banners and lists write it: `CUI//SP-PRVCY`. */
export function markingText(c: Collection): string {
  return [c.marking_label, ...c.cui_categories].join('//')
}
