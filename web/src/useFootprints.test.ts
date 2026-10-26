// The map fills each tile's {z}/{x}/{y} into the template itself, so they must reach it intact.
import { describe, expect, it } from 'vitest'
import { tileTemplate } from './useFootprints'

describe('tileTemplate', () => {
  it('keeps the placeholders MapLibre fills in, unencoded, under the API prefix', () => {
    const path = '/tiles/footprints/1ffbf3a5-bb99-4381-ba97-0ec4a4b30b25/{z}/{x}/{y}.mvt'

    expect(tileTemplate(path, 'https://ingest.example')).toBe(
      'https://ingest.example/api/tiles/footprints/1ffbf3a5-bb99-4381-ba97-0ec4a4b30b25/{z}/{x}/{y}.mvt',
    )
  })
})
