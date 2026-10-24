import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'
import type { components } from './generated/api'

// Response shapes come from the API's OpenAPI schema (`make api-types`), never retyped here.
type Schemas = components['schemas']
export type Collection = Schemas['Collection']
export type Marking = Schemas['Marking']
export type Me = Schemas['Me']
export type WebConfig = Schemas['WebConfig']

type State<T> = { data?: T; error?: string; loading: boolean }

/** GETs `/api{path}` with the signed-in user's access token. */
export function useApi<T>(path: string): State<T> {
  const auth = useAuth()
  const token = auth.user?.access_token
  const [state, setState] = useState<State<T>>({ loading: true })

  useEffect(() => {
    if (!token) return
    const controller = new AbortController()
    fetch(`/api${path}`, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
        setState({ data: (await response.json()) as T, loading: false })
      })
      .catch((error: Error) => {
        if (error.name !== 'AbortError') setState({ error: error.message, loading: false })
      })
    return () => controller.abort()
  }, [path, token])

  return state
}
