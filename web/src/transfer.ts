import { blockId, blockList, range, remaining, type Plan } from './blocks'

/** Storage refused or failed a request; `status` 0 when it never answered. */
export class StorageError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** The two Blob Storage calls an upload makes, through a signed URL. */
export type Storage = {
  stage: (url: string, id: string, body: Blob, signal: AbortSignal) => Promise<void>
  commit: (url: string, blockList: string, contentType: string | undefined, signal: AbortSignal) => Promise<void>
}

export type Transfer = {
  file: Blob
  plan: Plan
  staged: ReadonlySet<number> // blocks staged before, by an earlier attempt or page
  url: string
  contentType: string | undefined
  renew: () => Promise<string> // a fresh signed URL, once the last has expired
  onStaged: (index: number) => void
  signal: AbortSignal
}

export type Options = { storage: Storage; concurrency: number; sleep: (ms: number) => Promise<void> }

export const ATTEMPTS = 5 // per request, before the upload fails
const BACKOFF_MS = 500 // doubling after each failure

/** Worth trying again: storage didn't answer, was busy, or failed on its side. */
function transient(error: StorageError): boolean {
  return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500
}

/** Runs `request` with the current URL until it succeeds, renewing the URL when storage says
 * it has expired (403) and backing off after transient failures, at most `ATTEMPTS` times. */
async function withRetries(
  request: (url: string) => Promise<void>,
  url: { current: string; renew: () => Promise<string> },
  { sleep }: Options,
  signal: AbortSignal,
): Promise<void> {
  // Bounded: every failure either throws or uses up one of `ATTEMPTS`.
  for (let attempt = 1; ; attempt++) {
    try {
      return await request(url.current)
    } catch (error) {
      if (!(error instanceof StorageError) || signal.aborted || attempt >= ATTEMPTS) throw error
      if (error.status === 403) {
        url.current = await url.renew()
      } else if (transient(error)) {
        await sleep(BACKOFF_MS * 2 ** (attempt - 1))
      } else {
        throw error
      }
    }
  }
}

/** One renewal at a time: blocks failing together on an expired URL share the next one. */
function sharedRenewal(transfer: Transfer) {
  let pending: Promise<string> | undefined
  const url = {
    current: transfer.url,
    renew: () => {
      pending ??= transfer.renew().finally(() => (pending = undefined))
      return pending
    },
  }
  return url
}

/**
 * Stages the blocks of `transfer.file` not yet staged, `concurrency` at a time, then commits the
 * whole list in order. Resolves once the blob exists; rejects with the first error that retrying
 * can't fix, or when `transfer.signal` aborts.
 */
export async function upload(transfer: Transfer, options: Options): Promise<void> {
  const { file, plan, signal } = transfer
  const url = sharedRenewal(transfer)
  const queue = remaining(plan, transfer.staged)
  const worker = async () => {
    for (let index = queue.shift(); index !== undefined; index = queue.shift()) {
      signal.throwIfAborted()
      const [start, end] = range(plan, file.size, index)
      const id = blockId(index)
      await withRetries((u) => options.storage.stage(u, id, file.slice(start, end), signal), url, options, signal)
      transfer.onStaged(index)
    }
  }
  const workers = Array.from({ length: Math.min(options.concurrency, queue.length) }, worker)
  await Promise.all(workers)
  signal.throwIfAborted()
  const list = blockList(plan.count)
  await withRetries((u) => options.storage.commit(u, list, transfer.contentType, signal), url, options, signal)
}
