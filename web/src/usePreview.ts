import type { StacItem } from './api'
import { useTilerSession } from './useTilerSession'

/** A raster preview for the map: the tiler's TileJSON URL, and the token its requests carry. */
export type Preview = { tilejson: string; token: string }

/** Whether the tiler can draw an item: a placed raster with a COG, its own or derived. */
export function previewable(item: StacItem): boolean {
  const original = item.assets['original']
  return item.geometry !== null && (item.assets['cog'] !== undefined || original?.roles.includes('cloud-optimized') === true)
}

/** A preview of the item `itemId` while one is asked for (`useTilerSession`). */
export function usePreview(itemId: string | undefined): { preview: Preview | undefined; error: string | undefined } {
  const { session, error } = useTilerSession(itemId, 'preview')
  return { preview: session && { tilejson: session.url, token: session.token }, error }
}
