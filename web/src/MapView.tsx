import type { FeatureCollection, Point, Polygon } from 'geojson'
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
const DOTS = 'centres'
// Below this zoom a footprint can be too small to see or click, so its centre is marked too.
const DOTS_UNTIL = 6
const [BLUE, AMBER] = ['#1f5f8b', '#e0a100']
const LAYERS = ['fill', 'dots'] // the layers a pointer picks items from

type Props = {
  items: StacItem[]
  selected: string | undefined
  hovered: string | undefined
  onSelect: (id: string) => void
  onHover: (id: string | undefined) => void
  onViewport: (bbox: Bbox, view: View) => void
  initialView: View | undefined // read once, when the map is made
  fit: { bbox: Bbox } | undefined // each new request moves the map to show its bbox
}

const WORLD_VIEW: View = { lon: 0, lat: 20, zoom: 1.4 }

/** A bbox's area in square degrees, across the antimeridian too. */
function area([west, south, east, north]: Bbox): number {
  return ((east - west + 360) % 360 || 360) * (north - south)
}

/** The results' footprints, largest first so smaller ones draw, and are clicked, on top. */
function footprints(items: StacItem[]): FeatureCollection {
  const drawn = items.flatMap((item) => (item.geometry && item.bbox ? [{ item, size: area(item.bbox) }] : []))
  return {
    type: 'FeatureCollection',
    features: drawn
      .toSorted((a, b) => b.size - a.size)
      .map(({ item }) => ({ type: 'Feature', geometry: item.geometry as Polygon, properties: { id: item.id } })),
  }
}

/** The centre of each footprint's bbox, east of west across the antimeridian. */
function centres(items: StacItem[]): FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: items.flatMap((item) => {
      if (!item.geometry || !item.bbox) return []
      const [west, south, east, north] = item.bbox
      const lon = (west + (east < west ? east + 360 : east)) / 2
      const point: Point = { type: 'Point', coordinates: [lon > 180 ? lon - 360 : lon, (south + north) / 2] }
      return [{ type: 'Feature', geometry: point, properties: { id: item.id } }]
    }),
  }
}

function dotLayer(id: string, color: string, radius: number): maplibregl.CircleLayerSpecification {
  return {
    id,
    type: 'circle',
    source: DOTS,
    maxzoom: DOTS_UNTIL,
    paint: { 'circle-color': color, 'circle-radius': radius, 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5 },
    ...(id !== 'dots' && { filter: byId(undefined) }),
  }
}

/** The id of the topmost footprint under the pointer. */
function idAt(event: maplibregl.MapLayerMouseEvent): string | undefined {
  const id: unknown = event.features?.[0]?.properties['id']
  return typeof id === 'string' ? id : undefined
}

const byId = (id: string | undefined) => ['==', ['get', 'id'], id ?? ''] as maplibregl.FilterSpecification

/** Bounds MapLibre can fit; a bbox across the antimeridian continues east past 180. */
function bounds([west, south, east, north]: Bbox): LngLatBoundsLike {
  return [
    [west, south],
    [east < west ? east + 360 : east, north],
  ]
}

/** The search map: footprints of the results, kept in step with the viewport. */
export function MapView({ items, selected, hovered, onSelect, onHover, onViewport, initialView, fit }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<maplibregl.Map | undefined>(undefined)
  const [ready, setReady] = useState(false)
  // The map outlives renders; it calls whichever handlers are current.
  const handlers = useRef({ onSelect, onHover, onViewport, initialView })
  useEffect(() => {
    handlers.current = { onSelect, onHover, onViewport, initialView }
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
      m.addSource(DOTS, { type: 'geojson', data: centres([]) })
      m.addLayer({ id: 'fill', type: 'fill', source: SOURCE, paint: { 'fill-color': BLUE, 'fill-opacity': 0.12 } })
      m.addLayer({ id: 'line', type: 'line', source: SOURCE, paint: { 'line-color': BLUE, 'line-width': 1.25 } })
      m.addLayer({
        id: 'hovered',
        type: 'fill',
        source: SOURCE,
        paint: { 'fill-color': BLUE, 'fill-opacity': 0.3 },
        filter: byId(undefined),
      })
      m.addLayer({
        id: 'selected-fill',
        type: 'fill',
        source: SOURCE,
        paint: { 'fill-color': AMBER, 'fill-opacity': 0.2 },
        filter: byId(undefined),
      })
      m.addLayer({
        id: 'selected',
        type: 'line',
        source: SOURCE,
        paint: { 'line-color': AMBER, 'line-width': 3 },
        filter: byId(undefined),
      })
      m.addLayer(dotLayer('dots', BLUE, 4.5))
      m.addLayer(dotLayer('dots-hovered', BLUE, 7))
      m.addLayer(dotLayer('dots-selected', AMBER, 7))
      m.on('click', LAYERS, (event) => {
        const id = idAt(event)
        if (id) handlers.current.onSelect(id)
      })
      m.on('mousemove', LAYERS, (event) => {
        m.getCanvas().style.cursor = 'pointer'
        handlers.current.onHover(idAt(event))
      })
      m.on('mouseleave', LAYERS, () => {
        m.getCanvas().style.cursor = ''
        handlers.current.onHover(undefined)
      })
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
    if (!ready) return
    map.current?.getSource<GeoJSONSource>(SOURCE)?.setData(footprints(items))
    map.current?.getSource<GeoJSONSource>(DOTS)?.setData(centres(items))
  }, [ready, items])

  useEffect(() => {
    if (!ready) return
    map.current?.setFilter('selected', byId(selected))
    map.current?.setFilter('selected-fill', byId(selected))
    map.current?.setFilter('dots-selected', byId(selected))
  }, [ready, selected])

  useEffect(() => {
    if (!ready) return
    map.current?.setFilter('hovered', byId(hovered))
    map.current?.setFilter('dots-hovered', byId(hovered))
  }, [ready, hovered])

  useEffect(() => {
    if (ready && fit) map.current?.fitBounds(bounds(fit.bbox), { padding: 80, maxZoom: 14, duration: 600 })
  }, [ready, fit])

  return <div ref={container} className="map" />
}
