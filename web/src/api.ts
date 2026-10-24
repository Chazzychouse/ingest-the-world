import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

export type Marking = { code: string; label: string; banner_color: string }

export type Me = {
  subject: string
  name: string | null
  username: string | null
  roles: string[]
  groups: string[]
  clearance: Marking
}

export type Collection = {
  id: string
  title: string
  description: string
  marking: string
  marking_label: string
  marking_color: string
  cui_categories: string[]
  dissemination: string[]
  retention: { retain_days: number | null; legal_hold: boolean; tiering: { after_days: number; tier: string }[] }
  created_at: string
}

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
