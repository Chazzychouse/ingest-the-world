import type { Upload, UploadIn, UploadSession } from './api'
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
  | 'needs-file' // from an earlier page: pick the file again to carry on
  | 'processing' // in storage; the pipeline hasn't decided yet
  | 'finished' // the pipeline decided: see `outcome`
  | 'failed'

export type Entry = {
  key: string // the upload's id, or a local one for an upload the API never saw
  filename: string
  size: number
  collectionId: string
  phase: Phase
  stagedBlocks: number
  blocks: number
  problem?: string | undefined // why it's invalid or failed
  outcome?: Upload['status']
  reason?: string | null
  stale?: boolean // processing for longer than we keep asking
}

/** The API calls an upload makes. */
export type UploadApi = {
  create: (body: UploadIn) => Promise<UploadSession>
  renew: (id: string) => Promise<UploadSession>
  get: (id: string) => Promise<Upload>
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

/** Whether `file` is the one an upload was started from. */
function sameFile(saved: Saved, file: File): boolean {
  return file.name === saved.filename && file.size === saved.size && file.lastModified === saved.lastModified
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
 * This browser's uploads: one file transfers at a time (its blocks in parallel), each then
 * waits on the pipeline's outcome. Progress is logged so a reload can resume. Subscribe for
 * changes; `entries` is a new array after each one.
 */
export class Uploads {
  entries: Entry[] = []
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

  /** Picks up what an earlier page left: transfers wait for their file, the rest for the pipeline. */
  async restore(): Promise<void> {
    const saved = await this.deps.log.load()
    for (const s of saved.toSorted((a, b) => a.savedAt - b.savedAt)) {
      if (this.live.has(s.id)) continue
      const p = plan(s.size)
      const phase: Phase = s.stage === 'transferring' ? 'needs-file' : s.stage === 'processing' ? 'processing' : 'finished'
      const entry: Entry = {
        key: s.id,
        filename: s.filename,
        size: s.size,
        collectionId: s.collectionId,
        phase,
        stagedBlocks: s.stage === 'transferring' ? s.staged.length : p.count,
        blocks: p.count,
        ...(s.status && { outcome: s.status as Upload['status'], reason: s.reason ?? null }),
      }
      this.live.set(s.id, { entry, saved: s, polls: 0 })
    }
    this.changed()
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
    if (!live.saved) return // the API never created it: there's nothing to resume
    if (!live.file) return this.update(live, { phase: 'needs-file' })
    this.update(live, { phase: 'queued', problem: undefined })
    this.pump()
  }

  /** Carries on an upload from an earlier page with its file, picked again. */
  provideFile(key: string, file: File): void {
    const live = this.live.get(key)
    if (!live?.saved || live.entry.phase !== 'needs-file') return
    if (!sameFile(live.saved, file)) {
      return this.update(live, { problem: `${file.name} isn’t the file this upload started from` })
    }
    live.file = file
    this.update(live, { phase: 'queued', problem: undefined })
    this.pump()
  }

  /** Forgets an upload here. One already in storage carries on through the pipeline. */
  remove(key: string): void {
    const live = this.live.get(key)
    if (!live) return
    live.abort?.abort()
    this.live.delete(key)
    void this.deps.log.remove(key)
    this.changed()
    this.pump()
  }

  /** Asks again about an upload left processing for longer than we keep asking. */
  checkAgain(key: string): void {
    const live = this.live.get(key)
    if (!live || live.entry.phase !== 'processing') return
    live.polls = 0
    this.update(live, { stale: false })
  }

  /** Asks the API about every upload the pipeline hasn't decided yet. Call every few seconds. */
  async poll(): Promise<void> {
    const waiting = [...this.live.values()].filter((l) => l.entry.phase === 'processing' && !l.entry.stale)
    await Promise.all(waiting.map((live) => this.check(live)))
  }

  private async check(live: Live): Promise<void> {
    live.polls += 1
    if (live.polls > MAX_POLLS) return this.update(live, { stale: true })
    const latest = await this.deps.api.get(live.entry.key).catch(() => undefined)
    if (!latest || latest.status === 'pending') return
    this.update(live, { phase: 'finished', outcome: latest.status, reason: latest.reason })
    this.save(live, { stage: 'finished', staged: [], status: latest.status, reason: latest.reason })
  }

  private async create(file: File, collectionId: string, tags: string[]): Promise<void> {
    const blocks = plan(Math.max(file.size, 1)).count
    const base = { filename: file.name, size: file.size, collectionId, stagedBlocks: 0, blocks }
    const invalid = problem(file, tags)
    if (invalid) return this.track({ entry: { ...base, key: this.localKey(), phase: 'invalid', problem: invalid }, polls: 0 })
    const contentType = MEDIA_TYPE.test(file.type) ? file.type : undefined
    const body: UploadIn = { collection_id: collectionId, filename: file.name, size_bytes: file.size, tags }
    if (contentType) body.content_type = contentType
    try {
      const session = await this.deps.api.create(body)
      const live: Live = { entry: { ...base, key: session.upload.id, phase: 'queued' }, file, url: session.upload_url, polls: 0 }
      this.track(live)
      this.save(live, { stage: 'transferring', staged: [] })
    } catch (error) {
      this.track({ entry: { ...base, key: this.localKey(), phase: 'failed', problem: (error as Error).message }, polls: 0 })
    }
  }

  /** Starts the next queued transfer, if none is running. */
  private pump(): void {
    const all = [...this.live.values()]
    if (all.some((l) => l.entry.phase === 'uploading')) return
    const next = all.find((l) => l.entry.phase === 'queued')
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

  private save(live: Live, change: Pick<Saved, 'stage' | 'staged'> & Partial<Saved>): void {
    const { entry, file } = live
    live.saved = {
      id: entry.key,
      collectionId: entry.collectionId,
      filename: entry.filename,
      size: entry.size,
      lastModified: file?.lastModified ?? live.saved?.lastModified ?? 0,
      savedAt: live.saved?.savedAt ?? this.deps.now(),
      ...change,
    }
    void this.deps.log.put(live.saved)
  }

  private localKey(): string {
    this.localIds += 1
    return `local-${this.localIds}`
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
    this.entries = [...this.live.values()].map((l) => l.entry)
    for (const listener of this.listeners) listener()
  }
}

function describe(error: unknown): string {
  if (error instanceof StorageError) {
    return error.status === 0 ? 'Storage couldn’t be reached' : `Storage refused a block (${error.status})`
  }
  return error instanceof Error ? error.message : String(error)
}
