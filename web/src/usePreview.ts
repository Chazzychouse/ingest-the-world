import { useEffect, useRef, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { ApiError, postApi, type PreviewSession, type StacItem } from './api'

// A preview is reopened this long before its token expires, so the map never sends a dead one.
const RENEW_EARLY_MS = 60_000

/** A raster preview for the map: the tiler's TileJSON URL, and the token its requests carry. */
export type Preview = { tilejson: string; token: string }

/** Whether the tiler can draw an item: a placed raster with a COG, its own or derived. */
export function previewable(item: StacItem): boolean {
  const original = item.assets['original']
  return item.geometry !== null && (item.assets['cog'] !== undefined || original?.roles.includes('cloud-optimized') === true)
}

/**
 * A preview of the item `itemId` while one is asked for: the API opens (and audits) it, and it is
 * reopened before its token expires. Undefined while none is asked for or until it opens.
 */
export function usePreview(itemId: string | undefined): { preview: Preview | undefined; error: string | undefined } {
  const token = useAuth().user?.access_token
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
  }, [token])
  const signedIn = token !== undefined
  const [opened, setOpened] = useState<{ itemId: string; preview?: Preview; error?: string; expires: number }>()
  // Bumped to reopen the preview before it expires; part of the request, so it opens anew.
  const [generation, setGeneration] = useState(0)
  const request = itemId && JSON.stringify({ itemId, generation })

  useEffect(() => {
    const bearer = tokenRef.current
    if (!request || !signedIn || !bearer) return
    const { itemId: id } = JSON.parse(request) as { itemId: string }
    const controller = new AbortController()
    postApi<PreviewSession>(`/items/${id}/preview`, {}, bearer, controller.signal).then(
      (session) =>
        setOpened({
          itemId: id,
          preview: { tilejson: new URL(session.tilejson, window.location.href).href, token: session.token },
          expires: Date.parse(session.expires_at),
        }),
      (e: unknown) => {
        if (e instanceof Error && e.name === 'AbortError') return
        const message = e instanceof ApiError && e.status === 404 ? 'This file has no map preview.' : "The preview didn't open."
        setOpened({ itemId: id, error: message, expires: Infinity })
      },
    )
    return () => controller.abort()
  }, [request, signedIn])

  useEffect(() => {
    if (!opened?.preview) return
    const timer = setTimeout(() => setGeneration((g) => g + 1), Math.max(0, opened.expires - Date.now() - RENEW_EARLY_MS))
    return () => clearTimeout(timer)
  }, [opened])

  if (!itemId || opened?.itemId !== itemId) return { preview: undefined, error: undefined }
  return { preview: opened.preview, error: opened.error }
}
