import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { postApi, type ItemCollection, type SearchIn, type StacItem } from './api'
import { nextPage, PAGE_SIZE } from './search'

// The most results kept on screen; past this, narrowing the search beats scrolling.
export const MAX_RESULTS = 20 * PAGE_SIZE

// One empty list, so a search with nothing loaded yet reports the same items every render.
const NONE: StacItem[] = []

/** The pages loaded so far for one search, named by its first request. */
type Loaded = { key: string; items: StacItem[]; next: SearchIn | undefined; error?: string }

export type SearchState = {
  items: StacItem[]
  more: boolean // a next page exists and there's room for it
  loading: boolean
  error?: string
  loadMore: () => void
}

/**
 * Results of `body`, a page at a time, once `enabled`. A new body starts over (cancelling a page in flight, and
 * showing the previous results until the new ones arrive); `loadMore` follows the last page's
 * `next` link, so every match arrives once (SRCH-2).
 */
export function useSearch(body: SearchIn, { enabled }: { enabled: boolean }): SearchState {
  const token = useAuth().user?.access_token
  const key = JSON.stringify(body)
  const [loaded, setLoaded] = useState<Loaded | undefined>(undefined)
  const [loadingMore, setLoadingMore] = useState(false)
  const inflight = useRef<AbortController | undefined>(undefined)

  const fetchPage = useCallback(
    (request: SearchIn, onPage: (page: ItemCollection) => void, onError: (error: string) => void) => {
      if (!token) return
      inflight.current?.abort()
      const controller = new AbortController()
      inflight.current = controller
      postApi<ItemCollection>('/search', request, token, controller.signal)
        .then(onPage)
        .catch((error: Error) => {
          if (error.name !== 'AbortError') onError(error.message)
        })
    },
    [token],
  )

  useEffect(() => {
    if (!enabled) return
    const request = JSON.parse(key) as SearchIn
    fetchPage(
      request,
      (page) => setLoaded({ key, items: page.features, next: nextPage(request, page.links) }),
      (error) => setLoaded({ key, items: [], next: undefined, error }),
    )
    return () => inflight.current?.abort()
  }, [key, enabled, fetchPage])

  const current = loaded?.key === key ? loaded : undefined
  const request = current?.next
  const loadMore = useCallback(() => {
    if (!request) return
    setLoadingMore(true)
    const done = (update: (l: Loaded) => Loaded) => {
      setLoaded((l) => (l && l.key === key ? update(l) : l))
      setLoadingMore(false)
    }
    fetchPage(
      request,
      (page) => done((l) => ({ ...l, items: [...l.items, ...page.features], next: nextPage(request, page.links) })),
      (error) => done((l) => ({ ...l, error })),
    )
  }, [request, key, fetchPage])

  const items = loaded?.items ?? NONE
  return {
    items,
    more: current?.next !== undefined && items.length < MAX_RESULTS,
    loading: current === undefined || loadingMore,
    ...(loaded?.error && { error: loaded.error }),
    loadMore,
  }
}
