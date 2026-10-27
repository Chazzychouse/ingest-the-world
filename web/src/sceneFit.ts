// A margin around the sphere: it fills the view, short of touching its edges.
const MARGIN = 1.05
// From above and to one side: the model's shape reads better than straight on.
const DIRECTION: [number, number, number] = [0.55, 0.65, 0.52]

export type Placement = { position: [number, number, number]; near: number; far: number }

/** The camera placed to see a sphere whole, with clipping planes that hold all of it. */
export function fit(sphere: { center: [number, number, number]; radius: number }, fovDegrees: number): Placement {
  const [x, y, z] = sphere.center
  // Far enough that the sphere's angular radius is half the field of view, with the margin.
  const distance = (sphere.radius * MARGIN) / Math.sin((fovDegrees * Math.PI) / 360)
  const [dx, dy, dz] = DIRECTION
  const scale = distance / Math.hypot(dx, dy, dz)
  return {
    position: [x + dx * scale, y + dy * scale, z + dz * scale],
    // Close in, for depth precision, yet never inside the sphere.
    near: Math.min(distance / 1_000, (distance - sphere.radius) / 2),
    far: distance * 20 + sphere.radius * 4,
  }
}
