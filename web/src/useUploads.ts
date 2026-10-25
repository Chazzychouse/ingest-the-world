import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useAuth } from 'react-oidc-context'
import { getApi, postApi, type Upload, type UploadPage, type UploadSession } from './api'
import { blobStorage } from './blobStorage'
import { browserLog } from './uploadLog'
import { Uploads, type Entry } from './uploads'

const POLL_MS = 3000 // how often uploads in the pipeline are asked about; see `Uploads.poll`
const CONCURRENCY = 4 // blocks in flight per transfer

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * This browser's uploads, for the signed-in user. Made once per signed-in workspace, so a
 * transfer carries on while other tabs are shown; leaving the page mid-transfer asks first.
 */
export function useUploads(): { uploads: Uploads; entries: Entry[] } {
  const token = useAuth().user?.access_token
  // The manager outlives renders; its API calls use whichever token is current.
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
  }, [token])

  // oxlint-disable-next-line react/refs -- the token is read when an API call is made, not in render
  const [uploads] = useState(() => {
    const bearer = () => tokenRef.current ?? ''
    return new Uploads({
      api: {
        create: (body) => postApi<UploadSession>('/uploads', body, bearer()),
        renew: (id) => postApi<UploadSession>(`/uploads/${id}/url`, {}, bearer()),
        get: (id) => getApi<Upload>(`/uploads/${id}`, bearer()),
        list: (after) => getApi<UploadPage>(`/uploads${after ? `?token=${encodeURIComponent(after)}` : ''}`, bearer()),
      },
      storage: blobStorage,
      log: browserLog,
      sleep,
      now: Date.now,
      concurrency: CONCURRENCY,
    })
  })
  const entries = useSyncExternalStore(uploads.subscribe, () => uploads.entries)

  useEffect(() => {
    void uploads.restore()
    const timer = setInterval(() => void uploads.poll(), POLL_MS)
    return () => clearInterval(timer)
  }, [uploads])

  const transferring = entries.some((e) => e.phase === 'uploading' || e.phase === 'queued')
  useEffect(() => {
    if (!transferring) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [transferring])

  return { uploads, entries }
}
