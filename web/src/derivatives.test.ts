// ING-13: details say derivatives are being made only while the item says so, and name each that
// failed, with why.
import { describe, expect, it } from 'vitest'
import type { StacItem } from './api'
import { derivativeNotes } from './derivatives'

function item(derivatives: 'deriving' | 'ready', failures: StacItem['properties']['ingest:derivative_failures'] = {}): StacItem {
  return {
    properties: { 'ingest:derivatives': derivatives, 'ingest:derivative_failures': failures },
  } as StacItem
}

describe('derivativeNotes', () => {
  it('says nothing of a ready item with every derivative', () => {
    expect(derivativeNotes(item('ready'))).toEqual([])
  })

  it('says derivatives are on their way while the item derives', () => {
    expect(derivativeNotes(item('deriving'))).toEqual(['Previews and other files are still being made.'])
  })

  it('names each failed derivative, with why, in a fixed order', () => {
    const notes = derivativeNotes(item('ready', { thumbnail: 'unrenderable', cog: 'too-large' }))
    expect(notes).toEqual([
      'No map preview: the file is too large to convert.',
      "No thumbnail: the file couldn't be drawn.",
    ])
  })

  it('names a derivative it has no word for by its role', () => {
    expect(derivativeNotes(item('ready', { mesh: 'dead-lettered' }))).toEqual(['No mesh: making it kept failing.'])
  })
})
