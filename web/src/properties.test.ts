import { describe, expect, it } from 'vitest'
import { label, show } from './properties'

describe('label', () => {
  it('names the properties readers write, and leaves the rest as they are', () => {
    expect(label('ingest:extraction')).toBe('Header read')
    expect(label('file:checksum')).toBe('Checksum (SHA-256 multihash)')
    expect(label('vendor:thing')).toBe('vendor:thing')
  })
})

describe('show', () => {
  it('reads lists of columns and bands as their names', () => {
    expect(show('table:columns', [{ name: 'lat', type: 'double' }, { name: 'lon', type: 'double' }])).toBe('lat, lon')
    expect(show('raster:bands', [{ data_type: 'uint8' }, { data_type: 'uint8', nodata: 0 }])).toBe('uint8, uint8')
  })

  it('shows flags, grids, counts and empty lists in words', () => {
    expect(show('raster:cloud_optimized', false)).toBe('No')
    expect(show('proj:shape', [10980, 10980])).toBe('10980 × 10980')
    expect(show('mesh:extensions', [])).toBe('None')
    expect(show('pc:count', 1234567)).toBe((1234567).toLocaleString())
  })

  it('cuts a long value short', () => {
    expect(show('proj:wkt2', 'x'.repeat(500))).toHaveLength(161)
  })
})
