import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import type { components } from './generated/api'

// Response shapes come from the API's OpenAPI schema (`make api-types`), never retyped here.
type Schemas = components['schemas']
export type Collection = Schemas['Collection']
export type Marking = Schemas['Marking']
export type Me = Schemas['Me']
export type WebConfig = Schemas['WebConfig']
export type Kind = Schemas['Kind']
export type SearchIn = Schemas['SearchIn']
export type SearchFilters = Schemas['Filters']
export type FootprintView = Schemas['FootprintView']
export type StacItem = Schemas['StacItem']
export type Asset = Schemas['Asset']
export type AssetUrl = Schemas['AssetUrl']
export type PreviewSession = Schemas['PreviewSession']
export type ItemCollection = Schemas['ItemCollection']
export type NextLink = Schemas['NextLink']
export type UploadIn = Schemas['UploadIn']
export type Upload = Schemas['Upload']
export type UploadPage = Schemas['UploadPage']
export type Reason = Schemas['Reason']
export type UploadSession = Schemas['UploadSession']

type State<T> = { data?: T; error?: ApiError; loading: boolean }

/** A request the API answered with an error status, or that never reached it (status 0). */
export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init)
  if (!response.ok) throw new ApiError(response.status, `${response.status} ${response.statusText}`)
  return (await response.json()) as T
}

/** Sends `body` as JSON to `/api{path}` with `token`; the parsed response, or an Error. */
export function postApi<T>(path: string, body: unknown, token: string, signal?: AbortSignal) {
  return send<T>(path, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...(signal && { signal }),
  })
}

/** GETs `/api{path}` with `token`; the parsed response, or an Error. */
export function getApi<T>(path: string, token: string, signal?: AbortSignal) {
  return send<T>(path, { headers: { Authorization: `Bearer ${token}` }, ...(signal && { signal }) })
}

/** GETs `/api{path}` with the signed-in user's access token; nothing while `path` is undefined. */
export function useApi<T>(path: string | undefined): State<T> {
  const auth = useAuth()
  const token = auth.user?.access_token
  // Named by the path it answers, so a new path never shows the last one's data.
  const [state, setState] = useState<State<T> & { path?: string }>({ loading: true })

  useEffect(() => {
    if (!token || path === undefined) return
    const controller = new AbortController()
    getApi<T>(path, token, controller.signal)
      .then((data) => setState({ path, data, loading: false }))
      .catch((error: Error) => {
        if (error.name === 'AbortError') return
        setState({ path, error: error instanceof ApiError ? error : new ApiError(0, error.message), loading: false })
      })
    return () => controller.abort()
  }, [path, token])

  if (path === undefined) return { loading: false }
  return state.path === path ? state : { loading: true }
}
