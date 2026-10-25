import type { FeatureCollection, Polygon } from 'geojson'
import maplibregl, { type GeoJSONSource, type LngLatBoundsLike } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef, useState } from 'react'
import type { StacItem } from './api'
import { viewportBbox, type Bbox } from './search'

// Any MapLibre style: Azure Maps (atlas.azure.us in Government), self-hosted tiles, or this
// default demo style, which needs no key.
const STYLE: string = import.meta.env.VITE_BASEMAP_STYLE ?? 'https://demotiles.maplibre.org/style.json'
const SOURCE = 'footprints'

type Props = {
  items: StacItem[]
  selected: string | undefined
  onSelect: (id: string) => void
  onViewport: (bbox: Bbox) => void
}

function footprints(items: StacItem[]): FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: items.flatMap((item) =>
      item.geometry
        ? [{ type: 'Feature', geometry: item.geometry as Polygon, properties: { id: item.id } }]
        : [],
    ),
  }
}

/** Bounds MapLibre can fit; a bbox across the antimeridian continues east past 180. */
function bounds([west, south, east, north]: Bbox): LngLatBoundsLike {
  return [
    [west, south],
    [east < west ? east + 360 : east, north],
  ]
}

/** The search map: footprints of the results, kept in step with the viewport. */
export function MapView({ items, selected, onSelect, onViewport }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | undefined>(undefined)
  const [ready, setReady] = useState(false)
  // The map outlives renders; it calls whichever handlers are current.
  const handlers = useRef({ onSelect, onViewport })
  handlers.current = { onSelect, onViewport }

  useEffect(() => {
    if (!container.current) return
    const m = new maplibregl.Map({ container: container.current, style: STYLE, center: [0, 20], zoom: 1.4 })
    map.current = m
    const report = () => {
      const b = m.getBounds()
      handlers.current.onViewport(viewportBbox(b.getWest(), b.getSouth(), b.getEast(), b.getNorth()))
    }
    m.on('load', () => {
      m.addSource(SOURCE, { type: 'geojson', data: footprints([]) })
      m.addLayer({ id: 'fill', type: 'fill', source: SOURCE, paint: { 'fill-color': '#1f5f8b', 'fill-opacity': 0.18 } })
      m.addLayer({ id: 'line', type: 'line', source: SOURCE, paint: { 'line-color': '#1f5f8b', 'line-width': 1.5 } })
      m.addLayer({
        id: 'selected',
        type: 'line',
        source: SOURCE,
        paint: { 'line-color': '#e0a100', 'line-width': 3 },
        filter: ['==', ['get', 'id'], ''],
      })
      m.on('click', 'fill', (event) => {
        const id: unknown = event.features?.[0]?.properties['id']
        if (typeof id === 'string') handlers.current.onSelect(id)
      })
      m.on('mouseenter', 'fill', () => (m.getCanvas().style.cursor = 'pointer'))
      m.on('mouseleave', 'fill', () => (m.getCanvas().style.cursor = ''))
      m.on('moveend', report)
      setReady(true)
      report()
    })
    return () => {
      m.remove()
      map.current = undefined
      setReady(false)
    }
  }, [])

  useEffect(() => {
    if (ready) map.current?.getSource<GeoJSONSource>(SOURCE)?.setData(footprints(items))
  }, [ready, items])

  useEffect(() => {
    const m = map.current
    if (!ready || !m) return
    m.setFilter('selected', ['==', ['get', 'id'], selected ?? ''])
    const bbox = items.find((item) => item.id === selected)?.bbox
    if (bbox) m.fitBounds(bounds(bbox), { padding: 80, maxZoom: 14, duration: 600 })
    // Fit only when the selection changes, not when more results arrive.
  }, [ready, selected])

  return <div ref={container} className="map" />
}
