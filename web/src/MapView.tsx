import type { FeatureCollection, Polygon } from 'geojson'
import maplibregl, { type GeoJSONSource, type LngLatBoundsLike } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useRef, useState } from 'react'
import type { StacItem } from './api'
import { viewportBbox, type Bbox } from './search'
import type { View } from './searchUrl'

// Any MapLibre style: Azure Maps (atlas.azure.us in Government), self-hosted tiles, or this
// default demo style, which needs no key.
const STYLE: string = import.meta.env.VITE_BASEMAP_STYLE ?? 'https://demotiles.maplibre.org/style.json'
const SOURCE = 'footprints'

type Props = {
  items: StacItem[]
  selected: string | undefined
  onSelect: (id: string) => void
  onViewport: (bbox: Bbox, view: View) => void
  initialView: View | undefined // read once, when the map is made
  fit: { bbox: Bbox } | undefined // each new request moves the map to show its bbox
}

const WORLD_VIEW: View = { lon: 0, lat: 20, zoom: 1.4 }

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
export function MapView({ items, selected, onSelect, onViewport, initialView, fit }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | undefined>(undefined)
  const [ready, setReady] = useState(false)
  // The map outlives renders; it calls whichever handlers are current.
  const handlers = useRef({ onSelect, onViewport, initialView })
  useEffect(() => {
    handlers.current = { onSelect, onViewport, initialView }
  })

  useEffect(() => {
    if (!container.current) return
    const { lon, lat, zoom } = handlers.current.initialView ?? WORLD_VIEW
    const m = new maplibregl.Map({ container: container.current, style: STYLE, center: [lon, lat], zoom })
    map.current = m
    const report = () => {
      const b = m.getBounds()
      const centre = m.getCenter().wrap()
      handlers.current.onViewport(viewportBbox(b.getWest(), b.getSouth(), b.getEast(), b.getNorth()), {
        lon: centre.lng,
        lat: centre.lat,
        zoom: m.getZoom(),
      })
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
    if (ready) map.current?.setFilter('selected', ['==', ['get', 'id'], selected ?? ''])
  }, [ready, selected])

  useEffect(() => {
    if (ready && fit) map.current?.fitBounds(bounds(fit.bbox), { padding: 80, maxZoom: 14, duration: 600 })
  }, [ready, fit])

  return <div ref={container} className="map" />
}
