import type { Upload, UploadIn, UploadPage, UploadSession } from './api'
import { plan } from './blocks'
import { FILENAME, MAX_FILENAME_LENGTH, MAX_TAG_LENGTH, MAX_TAGS, MAX_UPLOAD_BYTES, MEDIA_TYPE } from './schema'
import { StorageError, upload, type Storage } from './transfer'
import type { Log, Saved } from './uploadLog'

/** Where an upload is, as the person who started it sees it. */
export type Phase =
  | 'invalid' // never sent: the API would refuse it
  | 'queued'
  | 'uploading'
  | 'paused'
  | 'needs-file' // from an earlier page or another browser: pick the file again to carry on
  | 'processing' // in storage; the pipeline hasn't decided yet (see `stage`)
  | 'finished' // the pipeline decided: see `outcome`
  | 'failed'

export type Entry = {
  key: string // the upload's id, or a local one for an upload the API never saw
  filename: string
  size: number
  collectionId: string
  at: number // when it was started, in ms: the list is newest first
  phase: Phase
  stagedBlocks: number
  blocks: number
  problem?: string | undefined // why it's invalid or failed
  stage?: Upload['stage'] // the API's word on a processing upload, or a cataloged one's derivatives
  attempts?: number
  outcome?: Upload['status']
  reason?: Upload['reason']
  signature?: string | null
  stale?: boolean // processing for longer than we keep asking
  elsewhere?: boolean // listed by the API, never transferred here: how much is staged is unknown
}

/** The API calls an upload makes. */
export type UploadApi = {
  create: (body: UploadIn) => Promise<UploadSession>
  renew: (id: string) => Promise<UploadSession>
  get: (id: string) => Promise<Upload>
  list: (token?: string) => Promise<UploadPage>
}

export type Deps = {
  api: UploadApi
  storage: Storage
  log: Log
  sleep: (ms: number) => Promise<void>
  now: () => number
  concurrency: number
}

// Polls of the pipeline, a few seconds apart, before an upload is left as "check back later".
export const MAX_POLLS = 200
// How often the list is asked for while nothing here is processing, to see uploads from elsewhere.
export const IDLE_LIST_MS = 30_000

const LOCAL = 'local-'

/** Whether the API never saw this upload, so only this page knows of it. */
export function isLocal(key: string): boolean {
  return key.startsWith(LOCAL)
}

/** Comma-separated tags as the API takes them: trimmed, distinct, none empty. */
export function parseTags(text: string): string[] {
  return [...new Set(text.split(',').map((t) => t.trim()).filter(Boolean))]
}

/** Why the API would refuse `file` with `tags`, or undefined if it wouldn't. */
export function problem(file: { name: string; size: number }, tags: string[]): string | undefined {
  if (file.size === 0) return 'is empty'
  if (file.size > MAX_UPLOAD_BYTES) return 'is larger than the 1 TiB limit'
  if (file.name.length > MAX_FILENAME_LENGTH) return `has a name longer than ${MAX_FILENAME_LENGTH} characters`
  if (!FILENAME.test(file.name)) return 'has a name storage can’t keep (control characters, or a trailing dot or space)'
  if (tags.length > MAX_TAGS) return `has more than ${MAX_TAGS} tags`
  if (tags.some((t) => t.length > MAX_TAG_LENGTH)) return `has a tag longer than ${MAX_TAG_LENGTH} characters`
  return undefined
}

/** Whether `file` is the one an upload was started from, as far as we know it. */
function sameFile(entry: Entry, saved: Saved | undefined, file: File): boolean {
  const modified = saved?.lastModified
  return file.name === entry.filename && file.size === entry.size && (modified === undefined || file.lastModified === modified)
}

/** What the API's word on an upload means here. */
function fromApi(known: Upload): Pick<Entry, 'phase' | 'stage' | 'attempts' | 'outcome' | 'reason' | 'signature'> {
  const { stage, attempts } = known
  if (known.status !== 'pending') {
    return { phase: 'finished', stage, attempts, outcome: known.status, reason: known.reason, signature: known.signature }
  }
  return { phase: stage === 'processing' ? 'processing' : 'needs-file', stage, attempts }
}

/** Whether the API has more to say about an upload: the pipeline's outcome, or its derivatives. */
function unsettled(entry: Entry): boolean {
  return entry.phase === 'processing' || (entry.phase === 'finished' && entry.stage === 'deriving')
}

type Live = {
  entry: Entry
  saved?: Saved
  file?: File
  url?: string | undefined // the signed URL from creation, used once
  abort?: AbortController | undefined
  polls: number
}

/**
 * The signed-in user's uploads: those this browser is moving, and those the API lists. One file
 * transfers at a time (its blocks in parallel), each then waits on the pipeline's outcome.
 * Progress is logged so a reload can resume. Subscribe for changes; `entries` is a new array
 * after each one.
 */
export class Uploads {
  entries: Entry[] = []
  hasOlder = false // the API has older uploads than those listed: see `loadOlder`
  private older: string | null | undefined // the token for them, once the list is first read
  private listedAt = -Infinity
  private readonly live = new Map<string, Live>()
  private readonly listeners = new Set<() => void>()
  private readonly deps: Deps
  private localIds = 0

  constructor(deps: Deps) {
    this.deps = deps
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Picks up what an earlier page left (transfers wait for their file), then what the API lists. */
  async restore(): Promise<void> {
    const saved = await this.deps.log.load()
    for (const s of saved) {
      // Finished ones are only in logs from earlier versions; the API lists them now.
      if (s.stage === 'finished') void this.deps.log.remove(s.id)
      if (s.stage === 'finished' || this.live.has(s.id)) continue
      const p = plan(s.size)
      const transferring = s.stage === 'transferring'
      const entry: Entry = {
        key: s.id,
        filename: s.filename,
        size: s.size,
        collectionId: s.collectionId,
        at: s.savedAt,
        phase: transferring ? 'needs-file' : 'processing',
        stagedBlocks: transferring ? s.staged.length : p.count,
        blocks: p.count,
      }
      this.live.set(s.id, { entry, saved: s, polls: 0 })
    }
    this.changed()
    await this.refresh()
  }

  /** Starts uploading `files` into `collectionId`, each tagged with `tags`. */
  async add(files: File[], collectionId: string, tags: string[]): Promise<void> {
    for (const file of files) await this.create(file, collectionId, tags)
    this.pump()
  }

  pause(key: string): void {
    const live = this.live.get(key)
    if (live?.entry.phase !== 'uploading' && live?.entry.phase !== 'queued') return
    this.update(live, { phase: 'paused' })
    live.abort?.abort()
  }

  resume(key: string): void {
    const live = this.live.get(key)
    if (live?.entry.phase !== 'paused' && live?.entry.phase !== 'failed') return
    if (isLocal(key)) return // the API never created it: there's nothing to resume
    if (!live.file) return this.update(live, { phase: 'needs-file' })
    this.update(live, { phase: 'queued', problem: undefined })
    this.pump()
  }

  /** Carries on an upload from an earlier page or another browser with its file, picked again. */
  provideFile(key: string, file: File): void {
    const live = this.live.get(key)
    if (live?.entry.phase !== 'needs-file') return
    if (!sameFile(live.entry, live.saved, file)) {
      return this.update(live, { problem: `${file.name} isn’t the file this upload started from` })
    }
    live.file = file
    this.update(live, { phase: 'queued', problem: undefined, elsewhere: false })
    this.pump()
  }

  /** Dismisses an upload the API never saw; the API keeps every other. */
  remove(key: string): void {
    const live = this.live.get(key)
    if (!live || !isLocal(key)) return
    this.live.delete(key)
    this.changed()
  }

  /** Asks again about an upload left processing for longer than we keep asking. */
  checkAgain(key: string): void {
    const live = this.live.get(key)
    if (!live || live.entry.phase !== 'processing') return
    live.polls = 0
    this.update(live, { stale: false })
  }

  /**
   * Asks the API about uploads the pipeline hasn't decided yet, through the list and then one by
   * one for any too old to be on its first page. Call every few seconds; while nothing is
   * processing, it asks only every `IDLE_LIST_MS`.
   */
  async poll(): Promise<void> {
    const waiting = [...this.live.values()].filter((l) => unsettled(l.entry) && !l.entry.stale)
    for (const live of waiting) {
      live.polls += 1
      if (live.polls > MAX_POLLS) this.update(live, { stale: true })
    }
    const asking = waiting.filter((l) => !l.entry.stale)
    if (asking.length === 0 && this.deps.now() - this.listedAt < IDLE_LIST_MS) return
    const listed = await this.refresh()
    const missed = asking.filter((l) => !listed.has(l.entry.key) && unsettled(l.entry))
    await Promise.all(missed.map((live) => this.check(live)))
  }

  /** Takes in an upload the API sent unasked, from its live event stream. */
  receive(known: Upload): void {
    this.merge(known)
  }

  /** Lists the next page of older uploads. */
  async loadOlder(): Promise<void> {
    if (!this.older) return
    const page = await this.deps.api.list(this.older).catch(() => undefined)
    if (!page) return
    for (const known of page.uploads) this.merge(known)
    this.older = page.next
    this.changed()
  }

  /** Merges the first page of the list; the ids it held, or none if the API couldn't be asked. */
  private async refresh(): Promise<Set<string>> {
    this.listedAt = this.deps.now()
    const page = await this.deps.api.list().catch(() => undefined)
    if (!page) return new Set()
    for (const known of page.uploads) this.merge(known)
    if (this.older === undefined) this.older = page.next
    this.changed()
    return new Set(page.uploads.map((u) => u.id))
  }

  private async check(live: Live): Promise<void> {
    const latest = await this.deps.api.get(live.entry.key).catch(() => undefined)
    if (latest) this.merge(latest)
  }

  /** Takes in the API's word on an upload. A transfer here knows its own progress better, until
   * the pipeline decides. */
  private merge(known: Upload): void {
    const live = this.live.get(known.id)
    if (!live) return this.track({ entry: this.listed(known), polls: 0 })
    const { phase } = live.entry
    const now = fromApi(known)
    if (now.phase === 'finished') {
      // Done, but a cataloged file's derivatives may still be on their way.
      if (phase === 'finished') return live.entry.stage === known.stage ? undefined : this.update(live, { stage: known.stage })
      live.abort?.abort()
      void this.deps.log.remove(known.id)
      return this.update(live, { ...now, stagedBlocks: live.entry.blocks, problem: undefined, stale: false })
    }
    if (phase === 'processing' || (phase === 'needs-file' && now.phase === 'processing')) {
      // Bytes this page committed may not be announced yet: they're processing all the same.
      this.update(live, { ...now, phase: 'processing' })
    }
  }

  /** An known only the API knew of. */
  private listed(known: Upload): Entry {
    const blocks = plan(known.size_bytes).count
    const now = fromApi(known)
    return {
      key: known.id,
      filename: known.filename,
      size: known.size_bytes,
      collectionId: known.collection_id,
      at: Date.parse(known.created_at),
      stagedBlocks: now.phase === 'needs-file' ? 0 : blocks,
      elsewhere: now.phase === 'needs-file',
      blocks,
      ...now,
    }
  }

  private async create(file: File, collectionId: string, tags: string[]): Promise<void> {
    const blocks = plan(Math.max(file.size, 1)).count
    const base = { filename: file.name, size: file.size, collectionId, stagedBlocks: 0, blocks, at: this.deps.now() }
    const invalid = problem(file, tags)
    if (invalid) return this.track({ entry: { ...base, key: this.localKey(), phase: 'invalid', problem: invalid }, polls: 0 })
    const contentType = MEDIA_TYPE.test(file.type) ? file.type : undefined
    const body: UploadIn = { collection_id: collectionId, filename: file.name, size_bytes: file.size, tags }
    if (contentType) body.content_type = contentType
    try {
      const session = await this.deps.api.create(body)
      const at = Date.parse(session.upload.created_at)
      const live: Live = { entry: { ...base, at, key: session.upload.id, phase: 'queued' }, file, url: session.upload_url, polls: 0 }
      this.track(live)
      this.save(live, { stage: 'transferring', staged: [] })
    } catch (error) {
      this.track({ entry: { ...base, key: this.localKey(), phase: 'failed', problem: (error as Error).message }, polls: 0 })
    }
  }

  /** Starts the next queued transfer, oldest first, if none is running. */
  private pump(): void {
    const all = [...this.live.values()]
    if (all.some((l) => l.entry.phase === 'uploading')) return
    const next = all.filter((l) => l.entry.phase === 'queued').toSorted((a, b) => a.entry.at - b.entry.at)[0]
    if (next) void this.transfer(next)
  }

  private async transfer(live: Live): Promise<void> {
    const { file, entry } = live
    if (!file) return
    const abort = new AbortController()
    live.abort = abort
    this.update(live, { phase: 'uploading' })
    const staged = new Set(live.saved?.staged ?? [])
    try {
      const url = live.url ?? (await this.renew(entry.key))
      live.url = undefined // used once; a resumed transfer asks for a fresh one
      await upload(
        {
          file,
          plan: plan(file.size),
          staged,
          url,
          contentType: MEDIA_TYPE.test(file.type) ? file.type : undefined,
          renew: () => this.renew(entry.key),
          onStaged: (index) => this.staged(live, staged, index),
          signal: abort.signal,
        },
        this.deps,
      )
      this.update(live, { phase: 'processing', stagedBlocks: entry.blocks })
      this.save(live, { stage: 'processing', staged: [] })
    } catch (error) {
      if (!abort.signal.aborted) this.update(live, { phase: 'failed', problem: describe(error) })
    } finally {
      live.abort = undefined
      this.pump()
    }
  }

  private async renew(id: string): Promise<string> {
    return (await this.deps.api.renew(id)).upload_url
  }

  private staged(live: Live, staged: Set<number>, index: number): void {
    staged.add(index)
    this.update(live, { stagedBlocks: staged.size })
    this.save(live, { stage: 'transferring', staged: [...staged] })
  }

  private save(live: Live, change: Pick<Saved, 'stage' | 'staged'>): void {
    const { entry, file } = live
    const lastModified = file?.lastModified ?? live.saved?.lastModified
    live.saved = {
      id: entry.key,
      collectionId: entry.collectionId,
      filename: entry.filename,
      size: entry.size,
      savedAt: live.saved?.savedAt ?? entry.at,
      ...(lastModified !== undefined && { lastModified }),
      ...change,
    }
    void this.deps.log.put(live.saved)
  }

  private localKey(): string {
    this.localIds += 1
    return `${LOCAL}${this.localIds}`
  }

  private track(live: Live): void {
    this.live.set(live.entry.key, live)
    this.changed()
  }

  private update(live: Live, change: Partial<Entry>): void {
    live.entry = { ...live.entry, ...change }
    this.changed()
  }

  private changed(): void {
    this.entries = [...this.live.values()].map((l) => l.entry).toSorted((a, b) => b.at - a.at)
    this.hasOlder = Boolean(this.older)
    for (const listener of this.listeners) listener()
  }
}

function describe(error: unknown): string {
  if (error instanceof StorageError) {
    return error.status === 0 ? 'Storage couldn’t be reached' : `Storage refused a block (${error.status})`
  }
  return error instanceof Error ? error.message : String(error)
}
