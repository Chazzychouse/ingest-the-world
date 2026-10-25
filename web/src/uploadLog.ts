// What this browser remembers about its uploads, so a reload can resume a transfer or keep
// waiting for the pipeline. Signed URLs are never kept: a resumed upload asks for a fresh one.

/** One upload as this browser left it. */
export type Saved = {
  id: string
  collectionId: string
  filename: string
  size: number
  lastModified: number // with name and size, tells the same file picked again
  stage: 'transferring' | 'processing' | 'finished'
  staged: number[] // blocks already staged, while transferring
  status?: string // the pipeline's outcome, once finished
  reason?: string | null
  savedAt: number
}

export type Log = {
  load: () => Promise<Saved[]>
  put: (saved: Saved) => Promise<void>
  remove: (id: string) => Promise<void>
}

const DB = 'ingest-uploads'
const STORE = 'uploads'

function request<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    r.addEventListener('success', () => resolve(r.result))
    r.addEventListener('error', () => reject(r.error ?? new Error('IndexedDB request failed')))
  })
}

function open(): Promise<IDBDatabase> {
  const opening = indexedDB.open(DB, 1)
  opening.addEventListener('upgradeneeded', () => opening.result.createObjectStore(STORE, { keyPath: 'id' }))
  return request(opening)
}

async function withStore<T>(mode: IDBTransactionMode, act: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open()
  try {
    return await request(act(db.transaction(STORE, mode).objectStore(STORE)))
  } finally {
    db.close()
  }
}

/** The log in IndexedDB. Where that's unavailable (a private window, blocked storage) uploads
 * still work; they just can't resume after a reload. */
export const browserLog: Log = {
  load: () => withStore('readonly', (s) => s.getAll() as IDBRequest<Saved[]>).catch(() => []),
  put: (saved) => withStore('readwrite', (s) => s.put(saved)).then(() => undefined, () => undefined),
  remove: (id) => withStore('readwrite', (s) => s.delete(id)).then(() => undefined, () => undefined),
}
