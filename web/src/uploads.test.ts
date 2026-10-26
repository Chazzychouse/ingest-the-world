// An upload goes from the file picked to the pipeline's outcome, survives a reload by resuming
// from its first unstaged block, and never sends the API what it would refuse.
import { describe, expect, it } from 'vitest'
import type { Upload, UploadIn } from './api'
import { BLOCK_BYTES, blockId } from './blocks'
import type { Storage } from './transfer'
import type { Log, Saved } from './uploadLog'
import { IDLE_LIST_MS, problem, Uploads, type UploadApi } from './uploads'

const ID = '6f1c1f0e-2a4b-4c3d-8e9f-0a1b2c3d4e5f'

function memoryLog(initial: Saved[] = []): Log & { rows: Map<string, Saved> } {
  const rows = new Map(initial.map((s) => [s.id, s]))
  return {
    rows,
    load: () => Promise.resolve([...rows.values()]),
    put: (s) => Promise.resolve(void rows.set(s.id, structuredClone(s))),
    remove: (id) => Promise.resolve(void rows.delete(id)),
  }
}

const PAGE = 2 // the fake API's page size, small enough to page

/** An upload as the API returns it, pending until changed. */
function record(over: Partial<Upload> & { id: string }): Upload {
  return {
    collection_id: 'c',
    filename: 'site.laz',
    size_bytes: 1,
    content_type: null,
    sha256: null,
    tags: [],
    status: 'pending',
    stage: 'awaiting-bytes',
    attempts: 0,
    reason: null,
    signature: null,
    marking: 'UNCLASSIFIED',
    marking_rank: 0,
    created_by: 'alice',
    created_at: '2026-10-25T00:00:00Z',
    ...over,
  }
}

const session = (upload: Upload) => Promise.resolve({ upload, upload_url: 'https://blob/x?sig', expires_at: '' })

/** The upload API over a map of uploads, newest first in pages of `PAGE`. */
function fakeApi(listed: Upload[] = []) {
  const created: UploadIn[] = []
  const server = new Map(listed.map((u) => [u.id, u]))
  let lists = 0
  const known = (id: string) => server.get(id) ?? record({ id })
  const api: UploadApi = {
    create: (body) => {
      created.push(body)
      const upload = record({ ...body, id: ID, tags: body.tags ?? [], content_type: body.content_type ?? null, sha256: null })
      server.set(ID, upload)
      return session(upload)
    },
    renew: (id) => session(known(id)),
    get: (id) => Promise.resolve(known(id)),
    list: (token) => {
      lists += 1
      const all = [...server.values()].toSorted((a, b) => b.created_at.localeCompare(a.created_at))
      const from = token ? Number(token) : 0
      const next = from + PAGE < all.length ? String(from + PAGE) : null
      return Promise.resolve({ uploads: all.slice(from, from + PAGE), next })
    },
  }
  const set = (id: string, change: Partial<Upload>) => server.set(id, { ...known(id), ...change })
  return { api, created, set, lists: () => lists }
}

function recording(): Storage & { ids: string[]; commits: number } {
  const storage = {
    ids: [] as string[],
    commits: 0,
    stage: (_url: string, id: string) => Promise.resolve(void storage.ids.push(id)),
    commit: () => Promise.resolve(void (storage.commits += 1)),
  }
  return storage
}

function uploads(api: UploadApi, storage: Storage, log: Log, now = () => 0) {
  return new Uploads({ api, storage, log, sleep: () => Promise.resolve(), now, concurrency: 2 })
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

function file(bytes: number, name = 'site.laz', lastModified = 1): File {
  return new File([new Uint8Array(bytes)], name, { lastModified })
}

describe('Uploads', () => {
  it('uploads a file and reports the outcome the pipeline decides', async () => {
    const { api, created, set } = fakeApi()
    const storage = recording()
    const log = memoryLog()
    const u = uploads(api, storage, log)
    await u.add([file(3 * BLOCK_BYTES)], 'site-survey', ['lidar'])
    await settle()
    expect(created).toEqual([{ collection_id: 'site-survey', filename: 'site.laz', size_bytes: 3 * BLOCK_BYTES, tags: ['lidar'] }])
    expect(storage.ids.toSorted()).toEqual([blockId(0), blockId(1), blockId(2)])
    expect(storage.commits).toBe(1)
    expect(u.entries[0]?.phase).toBe('processing')
    expect(log.rows.get(ID)?.stage).toBe('processing')

    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'processing', stage: 'awaiting-bytes' })
    set(ID, { stage: 'processing', attempts: 2 })
    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'processing', stage: 'processing', attempts: 2 })
    set(ID, { status: 'rejected', stage: null, attempts: 0, reason: 'duplicate' })
    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'finished', outcome: 'rejected', reason: 'duplicate' })
    expect(log.rows.has(ID)).toBe(false) // the API remembers it from here
  })

  it('follows a cataloged upload until its derivatives are made, from the event stream or polls', async () => {
    const { api, set } = fakeApi([record({ id: ID, status: 'cataloged', stage: 'deriving' })])
    const u = uploads(api, recording(), memoryLog())
    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'finished', outcome: 'cataloged', stage: 'deriving' })

    set(ID, { stage: 'ready' })
    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'finished', stage: 'ready' })

    const other = '7a2d2e1f-3b5c-4d4e-9f0a-1b2c3d4e5f60'
    u.receive(record({ id: other, status: 'cataloged', stage: 'deriving', created_at: '2026-10-26T00:00:00Z' }))
    u.receive(record({ id: other, status: 'cataloged', stage: 'ready', created_at: '2026-10-26T00:00:00Z' }))
    expect(u.entries.find((e) => e.key === other)).toMatchObject({ phase: 'finished', stage: 'ready' })
  })

  it('resumes after a reload once the same file is picked again, staging only what was missing', async () => {
    const { api } = fakeApi([record({ id: ID, size_bytes: 3 * BLOCK_BYTES })])
    const storage = recording()
    const saved: Saved = {
      id: ID,
      collectionId: 'c',
      filename: 'site.laz',
      size: 3 * BLOCK_BYTES,
      lastModified: 7,
      stage: 'transferring',
      staged: [0, 2],
      savedAt: 0,
    }
    const u = uploads(api, storage, memoryLog([saved]))
    await u.restore()
    expect(u.entries).toHaveLength(1)
    expect(u.entries[0]).toMatchObject({ phase: 'needs-file', stagedBlocks: 2, blocks: 3 })

    u.provideFile(ID, file(3 * BLOCK_BYTES, 'site.laz', 8))
    expect(u.entries[0]).toMatchObject({ phase: 'needs-file', problem: expect.stringContaining('isn’t the file') })

    u.provideFile(ID, file(3 * BLOCK_BYTES, 'site.laz', 7))
    await settle()
    expect(storage.ids).toEqual([blockId(1)])
    expect(u.entries[0]?.phase).toBe('processing')
  })

  it('lists uploads from elsewhere, newest first, and finishes one with its file', async () => {
    const elsewhere = '0b7e2d1c-0000-4000-8000-000000000001'
    const { api } = fakeApi([
      record({ id: elsewhere, filename: 'old.tif', status: 'quarantined', stage: null, signature: 'EICAR-Test-File' }),
      record({ id: ID, size_bytes: BLOCK_BYTES, created_at: '2026-10-25T01:00:00Z' }),
    ])
    const storage = recording()
    const u = uploads(api, storage, memoryLog())
    await u.restore()
    expect(u.entries.map((e) => [e.key, e.phase])).toEqual([
      [ID, 'needs-file'],
      [elsewhere, 'finished'],
    ])
    expect(u.entries[1]).toMatchObject({ outcome: 'quarantined', signature: 'EICAR-Test-File' })
    expect(u.entries[0]).toMatchObject({ elsewhere: true, stagedBlocks: 0 }) // its progress is unknown here

    u.provideFile(ID, file(BLOCK_BYTES + 1))
    expect(u.entries[0]?.problem).toContain('isn’t the file')
    u.provideFile(ID, file(BLOCK_BYTES, 'site.laz', 99)) // its date was never known here
    await settle()
    expect(storage.ids).toEqual([blockId(0)])
    expect(u.entries[0]).toMatchObject({ phase: 'processing', elsewhere: false })
  })

  it('pages back through older uploads', async () => {
    const ids = ['a', 'b', 'c'].map((n) => `0b7e2d1c-0000-4000-8000-00000000000${n}`)
    const done = { status: 'cataloged', stage: null } as const
    const { api } = fakeApi(ids.map((id, i) => record({ id, created_at: `2026-10-25T0${i}:00:00Z`, ...done })))
    const u = uploads(api, recording(), memoryLog())
    await u.restore()
    expect(u.entries.map((e) => e.key)).toEqual([ids[2], ids[1]])
    expect(u.hasOlder).toBe(true)

    await u.loadOlder()
    expect(u.entries.map((e) => e.key)).toEqual(ids.toReversed())
    expect(u.hasOlder).toBe(false)
  })

  it('asks for the list only now and then while nothing is processing', async () => {
    const { api, lists } = fakeApi()
    let now = 0
    const u = uploads(api, recording(), memoryLog(), () => now)
    await u.restore()
    await u.poll()
    expect(lists()).toBe(1)
    now = IDLE_LIST_MS
    await u.poll()
    expect(lists()).toBe(2)
  })

  it('never sends the API a file it would refuse', async () => {
    const { api, created } = fakeApi()
    const u = uploads(api, recording(), memoryLog())
    await u.add([file(0, 'empty.bin'), file(10, 'trailing.')], 'c', [])
    expect(created).toEqual([])
    expect(u.entries.map((e) => e.phase)).toEqual(['invalid', 'invalid'])
  })

  it('pauses and resumes a transfer', async () => {
    const { api } = fakeApi()
    const storage = recording()
    const u = uploads(api, storage, memoryLog())
    await u.add([file(BLOCK_BYTES)], 'c', [])
    u.pause(ID)
    await settle()
    expect(u.entries[0]?.phase).toBe('paused')
    u.resume(ID)
    await settle()
    expect(u.entries[0]?.phase).toBe('processing')
    expect(storage.commits).toBe(1)
  })
})

describe('problem', () => {
  it('names what the API would refuse', () => {
    expect(problem({ name: 'ok.tif', size: 1 }, [])).toBeUndefined()
    expect(problem({ name: 'a/b.tif', size: 1 }, [])).toBeDefined()
    expect(problem({ name: 'x'.repeat(256), size: 1 }, [])).toBeDefined()
    expect(problem({ name: 'ok.tif', size: 1 }, ['t'.repeat(65)])).toBeDefined()
  })
})
