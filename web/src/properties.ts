import { bytes } from './format'

// What a person calls each property the readers write. A key not here is shown as it is.
const LABELS: Record<string, string> = {
  datetime: 'Captured',
  start_datetime: 'Captured from',
  end_datetime: 'Captured until',
  created: 'Uploaded',
  'ingest:kind': 'Kind',
  'ingest:media_type': 'Format',
  'ingest:extraction': 'Header read',
  'file:size': 'Size',
  'file:checksum': 'Checksum (SHA-256 multihash)',
  'proj:code': 'Coordinate system',
  'proj:wkt2': 'Coordinate system (WKT)',
  'proj:shape': 'Grid (rows × columns)',
  'proj:bbox': 'Bounds, in its own CRS',
  'proj:transform': 'Geotransform',
  'raster:band_count': 'Bands',
  'raster:bands': 'Band types',
  'raster:cloud_optimized': 'Cloud-optimized',
  'raster:driver': 'GDAL driver',
  'table:row_count': 'Rows',
  'table:column_count': 'Columns',
  'table:columns': 'Column names',
  'geojson:features': 'Features',
  'geoparquet:geometry_column': 'Geometry column',
  'shp:shape_type': 'Shape type',
  'pc:count': 'Points',
  'pc:type': 'Point cloud type',
  'pc:encoding': 'Encoding',
  'pc:extent': 'Extent, in its own CRS',
  'las:version': 'LAS version',
  'las:point_format': 'LAS point format',
  'mesh:vertices': 'Vertices',
  'mesh:triangles': 'Triangles',
  'mesh:meshes': 'Meshes',
  'mesh:primitives': 'Primitives',
  'mesh:nodes': 'Nodes',
  'mesh:materials': 'Materials',
  'mesh:extensions': 'glTF extensions',
  'mesh:generator': 'Made by',
  'mesh:extent': 'Extent, in model units',
  bbox: 'Bounds (WGS84)',
  id: 'Item id',
}

const TIMES = new Set(['datetime', 'start_datetime', 'end_datetime', 'created'])
const COUNTS = new Set(['table:row_count', 'pc:count', 'mesh:vertices', 'mesh:triangles', 'geojson:features'])
// A value longer than this is cut short.
const MAX_CHARS = 160

// Values that are identifiers, numbers in a grid or source text: set in monospace.
const CODES = new Set(['id', 'bbox', 'file:checksum', 'proj:code', 'proj:wkt2', 'proj:bbox', 'proj:transform', 'pc:extent', 'mesh:extent'])

export function isCode(key: string): boolean {
  return CODES.has(key)
}

export function label(key: string): string {
  return LABELS[key] ?? key
}

function when(datetime: string): string {
  return new Date(datetime).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** The names in a list of `{name: …}`, or undefined when it isn't one. */
function names(value: unknown, key: 'name' | 'data_type'): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const found = value.map((v: unknown) => (typeof v === 'object' && v !== null ? (v as Record<string, unknown>)[key] : undefined))
  return found.every((n) => typeof n === 'string') ? (found as string[]) : undefined
}

/** One property's value, as a person reads it. */
export function show(key: string, value: unknown): string {
  if (key === 'file:size' && typeof value === 'number') return bytes(value)
  if (TIMES.has(key) && typeof value === 'string') return when(value)
  if (COUNTS.has(key) && typeof value === 'number') return value.toLocaleString()
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (key === 'proj:shape' && Array.isArray(value)) return value.join(' × ')
  const listed = key === 'table:columns' ? names(value, 'name') : key === 'raster:bands' ? names(value, 'data_type') : undefined
  if (listed) return listed.join(', ')
  if (Array.isArray(value) && value.length === 0) return 'None'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS)}…` : text
}
