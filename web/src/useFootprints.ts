import { useEffect, useRef, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { postApi, type FootprintView, type SearchFilters } from './api'

/**
 * The absolute URL template MapLibre fetches a view's tiles by, from the API's path for them. Not
 * built with `new URL`, which percent-encodes the `{z}/{x}/{y}` MapLibre fills in.
 */
export function tileTemplate(apiPath: string, origin: string = window.location.origin): string {
  return `${origin}/api${apiPath}`
}

// A view is reopened this long before it expires, so the map never asks for a dead one's tiles.
const RENEW_EARLY_MS = 60_000

/**
 * The absolute tile URL template for the footprints of everything matching `filters`, beyond the
 * results' pages: a footprint view the API opens (and audits) for them, reopened when they change and before
 * it expires. Undefined until the first one opens, or if it can't.
 */
export function useFootprints(filters: SearchFilters): string | undefined {
  const token = useAuth().user?.access_token
  // A refreshed token doesn't make the open view any less good; the next open uses it.
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
  }, [token])
  const signedIn = token !== undefined
  const key = JSON.stringify(filters)
  const [view, setView] = useState<{ key: string; tiles: string; expires: number } | undefined>()
  // Bumped to reopen the view before it expires; part of the request, so it opens anew.
  const [generation, setGeneration] = useState(0)
  const request = JSON.stringify({ filters, generation })

  useEffect(() => {
    const bearer = tokenRef.current
    if (!signedIn || !bearer) return
    const controller = new AbortController()
    const { filters: body } = JSON.parse(request) as { filters: SearchFilters }
    postApi<FootprintView>('/tiles/footprints', body, bearer, controller.signal).then(
      (opened) => {
        const [path] = opened.tiles
        const expires = Date.parse(opened.expires_at)
        setView(path === undefined ? undefined : { key: JSON.stringify(body), tiles: tileTemplate(path), expires })
      },
      () => setView(undefined), // the map keeps the results' own footprints
    )
    return () => controller.abort()
  }, [request, signedIn])

  useEffect(() => {
    if (!view) return
    const timer = setTimeout(() => setGeneration((g) => g + 1), Math.max(0, view.expires - Date.now() - RENEW_EARLY_MS))
    return () => clearTimeout(timer)
  }, [view])

  return view?.key === key ? view.tiles : undefined
}
