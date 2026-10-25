/** A byte count as people read it: `27.8 KB`, `105.4 MB`. */
export function bytes(size: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const power = Math.min(units.length - 1, Math.floor(Math.log(Math.max(size, 1)) / Math.log(1024)))
  return `${(size / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`
}
