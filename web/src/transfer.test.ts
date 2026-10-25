// An upload survives storage's transient failures and expired URLs, commits only once every
// block is staged, and resumes by staging only what's missing.
import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { BLOCK_BYTES, blockId, plan } from './blocks'
import { ATTEMPTS, StorageError, upload, type Storage } from './transfer'

type Call = { kind: 'stage' | 'commit'; url: string; id?: string; bytes?: number }

/** Storage that fails as `faults` says, in call order, and records what it was asked. */
function faulty(faults: number[]) {
  const calls: Call[] = []
  const valid = new Set(['url-0'])
  const answer = (call: Call) => {
    calls.push(call)
    const status = faults.shift() ?? 200
    if (!valid.has(call.url)) return Promise.reject(new StorageError(403, 'expired'))
    if (status !== 200) return Promise.reject(new StorageError(status, 'fault'))
    return Promise.resolve()
  }
  const storage: Storage = {
    stage: (url, id, body) => answer({ kind: 'stage', url, id, bytes: body.size }),
    commit: (url) => answer({ kind: 'commit', url }),
  }
  let renewals = 0
  const renew = () => {
    renewals += 1
    valid.add(`url-${renewals}`)
    return Promise.resolve(`url-${renewals}`)
  }
  return { storage, calls, renew, expire: () => valid.clear() }
}

const sizes = fc.integer({ min: 1, max: 6 * BLOCK_BYTES })
const fault = fc.constantFrom(200, 200, 200, 500, 503, 429, 0)

function run(bytes: number, store: ReturnType<typeof faulty>, staged: number[] = []) {
  const file = new Blob([new Uint8Array(bytes)])
  const done: number[] = []
  const result = upload(
    {
      file,
      plan: plan(bytes),
      staged: new Set(staged),
      url: 'url-0',
      contentType: undefined,
      renew: store.renew,
      onStaged: (i) => done.push(i),
      signal: new AbortController().signal,
    },
    { storage: store.storage, concurrency: 3, sleep: () => Promise.resolve() },
  )
  return { result, done }
}

describe('upload', () => {
  it('stages every block once and commits them all, through transient failures', async () => {
    await fc.assert(
      fc.asyncProperty(sizes, fc.array(fault, { maxLength: 3 }), async (bytes, faults) => {
        const store = faulty(faults)
        const { result, done } = run(bytes, store)
        await result
        const p = plan(bytes)
        expect(done.toSorted((a, b) => a - b)).toEqual(Array.from({ length: p.count }, (_, i) => i))
        const commits = store.calls.filter((c) => c.kind === 'commit')
        expect(commits.at(-1)).toEqual({ kind: 'commit', url: 'url-0' })
        // Nothing is staged after the commit starts.
        const first = store.calls.findIndex((c) => c.kind === 'commit')
        expect(store.calls.slice(first).every((c) => c.kind === 'commit')).toBe(true)
        const staged = store.calls.filter((c) => c.kind === 'stage').reduce((n, c) => n + (c.bytes ?? 0), 0)
        expect(staged).toBeGreaterThanOrEqual(bytes)
      }),
    )
  })

  it('resumes by staging only the blocks not yet staged', async () => {
    const bytes = 5 * BLOCK_BYTES
    const store = faulty([])
    await run(bytes, store, [0, 2, 3]).result
    const ids = store.calls.filter((c) => c.kind === 'stage').map((c) => c.id)
    expect(ids.toSorted()).toEqual([blockId(1), blockId(4)].toSorted())
  })

  it('renews an expired URL once for the blocks that hit it, and carries on', async () => {
    const store = faulty([])
    store.expire()
    await run(4 * BLOCK_BYTES, store).result
    expect(store.calls.filter((c) => c.kind === 'commit').map((c) => c.url)).toEqual(['url-1'])
  })

  it('gives up after a bounded number of attempts', async () => {
    const store = faulty(Array.from({ length: ATTEMPTS }, () => 503))
    await expect(run(1, store).result).rejects.toThrow(StorageError)
    expect(store.calls).toHaveLength(ATTEMPTS)
  })

  it("doesn't retry what retrying can't fix", async () => {
    const store = faulty([400])
    await expect(run(1, store).result).rejects.toMatchObject({ status: 400 })
    expect(store.calls).toHaveLength(1)
  })
})
