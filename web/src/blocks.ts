// How a file is cut into Blob Storage blocks: staged one by one (Put Block), then committed in
// order (Put Block List), which is what makes the blob exist and the pipeline see it.

const MIB = 1024 * 1024
export const BLOCK_BYTES = 8 * MIB // the spec's smallest block: retries stay cheap
export const MAX_BLOCKS = 50_000 // Put Block List's limit
export const MAX_BLOCK_BYTES = 4000 * MIB // Put Block's limit
const ID_DIGITS = String(MAX_BLOCKS - 1).length

/** A file's blocks: `count` of them, each `size` bytes but the last. */
export type Plan = { size: number; count: number }

/** The blocks for a file of `bytes` (> 0): 8 MiB each, or larger whole MiB when 50,000 of
 * those wouldn't hold it. */
export function plan(bytes: number): Plan {
  const size = Math.max(BLOCK_BYTES, Math.ceil(bytes / MAX_BLOCKS / MIB) * MIB)
  return { size, count: Math.ceil(bytes / size) }
}

/** The byte range `[start, end)` of block `index` in a file of `bytes`. */
export function range(p: Plan, bytes: number, index: number): [number, number] {
  const start = index * p.size
  return [start, Math.min(bytes, start + p.size)]
}

/** Block `index`'s id. Every id in a blob must be the same length before it's base64-encoded. */
export function blockId(index: number): string {
  return btoa(String(index).padStart(ID_DIGITS, '0'))
}

/** The Put Block List body committing blocks 0 to `count - 1`, in order. */
export function blockList(count: number): string {
  const ids = Array.from({ length: count }, (_, i) => `<Latest>${blockId(i)}</Latest>`)
  return `<?xml version="1.0" encoding="utf-8"?><BlockList>${ids.join('')}</BlockList>`
}

/** The blocks still to stage, given those already staged. */
export function remaining(p: Plan, staged: ReadonlySet<number>): number[] {
  return Array.from({ length: p.count }, (_, i) => i).filter((i) => !staged.has(i))
}
