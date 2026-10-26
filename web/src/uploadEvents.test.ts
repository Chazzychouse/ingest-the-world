import fc from 'fast-check'
import { describe, expect, it } from 'vitest'
import { parseEvents } from './uploadEvents'

const frame = (name: string, data: string) => `event: ${name}\ndata: ${data}\n\n`

describe('parseEvents', () => {
  it('reads whole events and keeps the start of the next', () => {
    const { events, rest } = parseEvents(`retry: 3000\n\n${frame('upload', '{"id":"a"}')}: keep-alive\n\nevent: up`)
    expect(events).toEqual([{ name: 'upload', data: '{"id":"a"}' }])
    expect(rest).toBe('event: up')
  })

  it('keeps line breaks JSON leaves unescaped inside the data', () => {
    const data = JSON.stringify({ filename: 'a b c' })
    expect(parseEvents(frame('upload', data)).events).toEqual([{ name: 'upload', data }])
  })

  it('reads the same events however the stream is cut into chunks', () => {
    const datas = fc.array(fc.json().map((j) => JSON.stringify(JSON.parse(j))), { maxLength: 5 })
    fc.assert(
      fc.property(datas, fc.array(fc.nat(), { maxLength: 8 }), (sent, cuts) => {
        const text = sent.map((d) => frame('upload', d)).join(': keep-alive\n\n')
        const points = [...new Set(cuts.map((c) => c % (text.length + 1)))].toSorted((a, b) => a - b)
        const chunks = [0, ...points].map((start, i) => text.slice(start, points[i] ?? text.length))
        let buffer = ''
        const got: string[] = []
        for (const chunk of chunks) {
          const { events, rest } = parseEvents(buffer + chunk)
          buffer = rest
          got.push(...events.map((e) => e.data))
        }
        expect(got).toEqual(sent)
      }),
    )
  })
})
