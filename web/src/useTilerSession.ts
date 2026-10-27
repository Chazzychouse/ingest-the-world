import { useEffect, useRef, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { ApiError, postApi, type PreviewSession, type SceneSession } from './api'

// A session is reopened this long before its token expires, so nothing ever sends a dead one.
const RENEW_EARLY_MS = 60_000

/** What the tiler serves an item as: a raster preview for the map, or a scene of its 3D Tiles. */
export type SessionKind = 'preview' | 'scene'

/** Where the tiler serves the item (its TileJSON or its tileset), and the token every request
 * to it carries. */
export type TilerSession = { url: string; token: string }

const MISSING: Record<SessionKind, string> = {
  preview: 'This file has no map preview.',
  scene: 'This file has no 3D view.',
}
const FAILED: Record<SessionKind, string> = {
  preview: "The preview didn't open.",
  scene: "The 3D view didn't open.",
}

function urlOf(session: PreviewSession | SceneSession): string {
  return 'tilejson' in session ? session.tilejson : session.tileset
}

/**
 * A tiler session for the item `itemId` while one is asked for: the API opens (and audits) it,
 * and it is reopened before its token expires. Undefined while none is asked for or until it
 * opens.
 */
export function useTilerSession(
  itemId: string | undefined,
  kind: SessionKind,
): { session: TilerSession | undefined; error: string | undefined } {
  const token = useAuth().user?.access_token
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
  }, [token])
  const signedIn = token !== undefined
  const [opened, setOpened] = useState<{ itemId: string; session?: TilerSession; error?: string; expires: number }>()
  // Bumped to reopen the session before it expires; part of the request, so it opens anew.
  const [generation, setGeneration] = useState(0)
  const request = itemId && JSON.stringify({ itemId, generation })

  useEffect(() => {
    const bearer = tokenRef.current
    if (!request || !signedIn || !bearer) return
    const { itemId: id } = JSON.parse(request) as { itemId: string }
    const controller = new AbortController()
    postApi<PreviewSession | SceneSession>(`/items/${id}/${kind}`, {}, bearer, controller.signal).then(
      (session) =>
        setOpened({
          itemId: id,
          session: { url: new URL(urlOf(session), window.location.href).href, token: session.token },
          expires: Date.parse(session.expires_at),
        }),
      (e: unknown) => {
        if (e instanceof Error && e.name === 'AbortError') return
        const message = e instanceof ApiError && e.status === 404 ? MISSING[kind] : FAILED[kind]
        setOpened({ itemId: id, error: message, expires: Infinity })
      },
    )
    return () => controller.abort()
  }, [request, signedIn, kind])

  useEffect(() => {
    if (!opened?.session) return
    const timer = setTimeout(() => setGeneration((g) => g + 1), Math.max(0, opened.expires - Date.now() - RENEW_EARLY_MS))
    return () => clearTimeout(timer)
  }, [opened])

  if (!itemId || opened?.itemId !== itemId) return { session: undefined, error: undefined }
  return { session: opened.session, error: opened.error }
}
