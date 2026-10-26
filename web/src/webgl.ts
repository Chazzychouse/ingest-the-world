/** Whether this browser can draw the map: MapLibre needs WebGL, which a browser may have turned
 * off or lack the graphics for. Without it the map's constructor throws. */
export function canDrawMaps(): boolean {
  const canvas = document.createElement('canvas')
  return (canvas.getContext('webgl2') ?? canvas.getContext('webgl')) !== null
}
