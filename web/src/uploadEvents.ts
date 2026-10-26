import type { Upload } from './api'

/** A server-sent event: its name and its data, lines joined. */
export type ServerEvent = { name: string; data: string }

/**
 * The whole events in `buffer`, and what's left of it: the start of an event still arriving.
 * Lines end at "\n" alone (the API never sends "\r"), so a line break JSON leaves unescaped,
 * such as U+2028 in a filename, stays inside its data.
 */
export function parseEvents(buffer: string): { events: ServerEvent[]; rest: string } {
  const blocks = buffer.split('\n\n')
  const rest = blocks.pop() ?? ''
  const events = blocks.flatMap((block) => {
    let name = 'message'
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event: ')) name = line.slice('event: '.length)
      else if (line.startsWith('data: ')) data.push(line.slice('data: '.length))
    }
    return data.length > 0 ? [{ name, data: data.join('\n') }] : [] // comments and `retry:` carry none
  })
  return { events, rest }
}

/**
 * Follows the caller's uploads through `GET /api/uploads/events` until the API ends the stream
 * (after a few minutes) or `signal` aborts, passing each upload to `onUpload`. Rejects if the
 * stream can't be opened or breaks.
 */
export async function followUploads(token: string, onUpload: (upload: Upload) => void, signal: AbortSignal) {
  const response = await fetch('/api/uploads/events', { headers: { Authorization: `Bearer ${token}` }, signal })
  if (!response.ok || !response.body) throw new Error(`${response.status} ${response.statusText}`)
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) return
    const { events, rest } = parseEvents(buffer + value)
    buffer = rest
    for (const event of events) if (event.name === 'upload') onUpload(JSON.parse(event.data) as Upload)
  }
}
