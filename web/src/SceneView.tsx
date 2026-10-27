import { TilesRenderer } from '3d-tiles-renderer'
import { useEffect, useRef, useState } from 'react'
import {
  DirectionalLight,
  HemisphereLight,
  PerspectiveCamera,
  Scene,
  Sphere,
  WebGLRenderer,
} from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { fit } from './sceneFit'
import { useTilerSession } from './useTilerSession'

// How much screen-space error a tile may show before its children replace it, in pixels.
const ERROR_TARGET = 12
// The stage behind the model: the viewer's background (`.scene` in index.css).
const STAGE = 0x14171c

/** Every request for the tileset and its tiles carries the scene's token. */
function bearer(token: string): RequestInit {
  return { headers: { Authorization: `Bearer ${token}` } }
}

/** A mesh's 3D Tiles, streamed from the tiler under a scene the API opens (and audits). */
export function SceneView({ itemId, title, onClose }: { itemId: string; title: string; onClose: () => void }) {
  const { session, error } = useTilerSession(itemId, 'scene')
  const holder = useRef<HTMLDivElement>(null)
  const tilesRef = useRef<TilesRenderer>(null)
  const [status, setStatus] = useState<string>('Opening…')
  const tileset = session?.url
  const token = session?.token
  // The newest token, for the renderer's first request as for every later one.
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
    if (tilesRef.current && token) tilesRef.current.fetchOptions = bearer(token)
  }, [token])

  // The renderer, built once per tileset; a renewed token only changes what requests carry.
  useEffect(() => {
    const element = holder.current
    if (!tileset || !element) return
    const renderer = new WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setClearColor(STAGE)
    element.appendChild(renderer.domElement)
    const scene = new Scene()
    const sun = new DirectionalLight(0xffffff, 1.4)
    sun.position.set(1, 2, 1)
    scene.add(new HemisphereLight(0xffffff, 0x445566, 2.2), sun)
    const camera = new PerspectiveCamera(45, 1, 0.1, 1e7)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    const tiles = new TilesRenderer(tileset)
    tilesRef.current = tiles
    if (tokenRef.current) tiles.fetchOptions = bearer(tokenRef.current)
    tiles.errorTarget = ERROR_TARGET
    // A tileset's glTF content is Z up (3D Tiles); three.js is Y up.
    tiles.group.rotation.x = -Math.PI / 2
    scene.add(tiles.group)
    tiles.setCamera(camera)

    let fitted = false
    tiles.addEventListener('load-tileset', () => {
      const sphere = new Sphere()
      if (fitted || !tiles.getBoundingSphere(sphere)) return
      fitted = true
      tiles.group.updateMatrixWorld(true)
      sphere.applyMatrix4(tiles.group.matrixWorld)
      const placed = fit({ center: sphere.center.toArray(), radius: sphere.radius }, camera.fov)
      camera.position.fromArray(placed.position)
      camera.near = placed.near
      camera.far = placed.far
      camera.updateProjectionMatrix()
      controls.target.copy(sphere.center)
      controls.update()
      setStatus('')
    })
    tiles.addEventListener('load-error', () => setStatus("Some of the model didn't load."))

    const resize = () => {
      const { clientWidth: width, clientHeight: height } = element
      renderer.setSize(width, height)
      camera.aspect = width / Math.max(height, 1)
      camera.updateProjectionMatrix()
      tiles.setResolutionFromRenderer(camera, renderer)
    }
    const observer = new ResizeObserver(resize)
    observer.observe(element)
    resize()
    renderer.setAnimationLoop(() => {
      controls.update()
      camera.updateMatrixWorld()
      tiles.update()
      renderer.render(scene, camera)
    })
    return () => {
      renderer.setAnimationLoop(null)
      observer.disconnect()
      controls.dispose()
      tiles.dispose()
      renderer.dispose()
      renderer.domElement.remove()
      tilesRef.current = null
    }
  }, [tileset])

  useEffect(() => {
    const close = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [onClose])

  return (
    <div className="scene" role="dialog" aria-modal="true" aria-label={`3D view of ${title}`}>
      <header>
        <h2>{title}</h2>
        <span className="muted">Drag to orbit · right-drag to pan · scroll to zoom</span>
        <button className="secondary" onClick={onClose}>
          Close
        </button>
      </header>
      <div className="scene-canvas" ref={holder} />
      {(error || status) && (
        <p className={error ? 'error scene-status' : 'muted scene-status'} role={error ? 'alert' : 'status'}>
          {error ?? status}
        </p>
      )}
    </div>
  )
}

export default SceneView
