// An upload goes from the file picked to the pipeline's outcome, survives a reload by resuming
// from its first unstaged block, and never sends the API what it would refuse.
import { describe, expect, it } from 'vitest'
import type { Upload, UploadIn } from './api'
import { BLOCK_BYTES, blockId } from './blocks'
import type { Storage } from './transfer'
import type { Log, Saved } from './uploadLog'
import { problem, Uploads, type UploadApi } from './uploads'

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

function fakeApi(status: () => Upload['status']) {
  const created: UploadIn[] = []
  const upload = (body: UploadIn): Upload => ({
    ...body,
    content_type: body.content_type ?? null,
    sha256: null,
    tags: body.tags ?? [],
    id: ID,
    status: status(),
    reason: null,
    marking: 'UNCLASSIFIED',
    marking_rank: 0,
    created_by: 'alice',
    created_at: '2026-10-25T00:00:00Z',
  })
  const session = (body: UploadIn) => ({ upload: upload(body), upload_url: 'https://blob/x?sig', expires_at: '' })
  const api: UploadApi = {
    create: (body) => {
      created.push(body)
      return Promise.resolve(session(body))
    },
    renew: () => Promise.resolve(session({ collection_id: 'c', filename: 'f', size_bytes: 1 })),
    get: () => Promise.resolve(upload({ collection_id: 'c', filename: 'f', size_bytes: 1 })),
  }
  return { api, created }
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

function uploads(api: UploadApi, storage: Storage, log: Log) {
  return new Uploads({ api, storage, log, sleep: () => Promise.resolve(), now: () => 0, concurrency: 2 })
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

function file(bytes: number, name = 'site.laz', lastModified = 1): File {
  return new File([new Uint8Array(bytes)], name, { lastModified })
}

describe('Uploads', () => {
  it('uploads a file and reports the outcome the pipeline decides', async () => {
    let status: Upload['status'] = 'pending'
    const { api, created } = fakeApi(() => status)
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
    expect(u.entries[0]?.phase).toBe('processing')
    status = 'cataloged'
    await u.poll()
    expect(u.entries[0]).toMatchObject({ phase: 'finished', outcome: 'cataloged' })
    expect(log.rows.get(ID)?.stage).toBe('finished')
  })

  it('resumes after a reload once the same file is picked again, staging only what was missing', async () => {
    const { api } = fakeApi(() => 'pending')
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
    expect(u.entries[0]).toMatchObject({ phase: 'needs-file', stagedBlocks: 2, blocks: 3 })

    u.provideFile(ID, file(3 * BLOCK_BYTES, 'site.laz', 8))
    expect(u.entries[0]).toMatchObject({ phase: 'needs-file', problem: expect.stringContaining('isn’t the file') })

    u.provideFile(ID, file(3 * BLOCK_BYTES, 'site.laz', 7))
    await settle()
    expect(storage.ids).toEqual([blockId(1)])
    expect(u.entries[0]?.phase).toBe('processing')
  })

  it('never sends the API a file it would refuse', async () => {
    const { api, created } = fakeApi(() => 'pending')
    const u = uploads(api, recording(), memoryLog())
    await u.add([file(0, 'empty.bin'), file(10, 'trailing.')], 'c', [])
    expect(created).toEqual([])
    expect(u.entries.map((e) => e.phase)).toEqual(['invalid', 'invalid'])
  })

  it('pauses and resumes a transfer', async () => {
    const { api } = fakeApi(() => 'pending')
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
