import { useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { ApiError, getApi, useApi, type Asset, type AssetUrl, type Marking, type StacItem } from './api'
import { bytes } from './format'
import { KindIcon } from './KindIcon'

// The asset key of an item's original; every other asset is a derivative, keyed by its role.
const ORIGINAL = 'original'

/** The path that answers with a short-lived link to an asset's bytes. */
function linkPath(asset: Asset): string {
  return `${asset.href}/url`
}

/**
 * An item's thumbnail, through a link the API signs for it; its kind's drawing while there is
 * none, or if it doesn't load.
 */
export function Thumbnail({ item, className, alt }: { item: StacItem; className: string; alt: string }) {
  const asset = item.assets.thumbnail
  const signed = useApi<AssetUrl>(asset && linkPath(asset))
  const [failed, setFailed] = useState<string>()
  const url = signed.data?.url
  if (!url || failed === url) return <KindIcon kind={item.properties['ingest:kind']} className={`${className} none`} />
  return <img className={className} src={url} alt={alt} loading="lazy" onError={() => setFailed(url)} />
}

/** The original first, then each derivative by name. */
function ordered(item: StacItem): [string, Asset][] {
  return Object.entries(item.assets).toSorted(([a], [b]) =>
    a === ORIGINAL ? -1 : b === ORIGINAL ? 1 : a.localeCompare(b),
  )
}

/** An item's files, each downloaded through a link the API signs at the moment it's asked for,
 * and each carrying the item's marking: a derivative is marked as its original is. */
export function Files({ item, marking }: { item: StacItem; marking: Marking }) {
  const token = useAuth().user?.access_token
  const [error, setError] = useState<string>()
  const download = (asset: Asset) => {
    if (!token) return
    setError(undefined)
    getApi<AssetUrl>(linkPath(asset), token).then(
      ({ url }) => window.location.assign(url),
      (e: unknown) => setError(`${asset.title} didn't download: ${e instanceof ApiError ? e.message : String(e)}`),
    )
  }
  return (
    <section className="files" aria-label="Files">
      <h3>Files</h3>
      <ul>
        {ordered(item).map(([key, asset]) => (
          <li key={key}>
            <button className="link" onClick={() => download(asset)}>
              {key === ORIGINAL ? 'Original' : asset.title}
            </button>
            <span className="meta">
              <span className="chip" style={{ background: marking.banner_color }}>
                {marking.label}
              </span>
              {asset.type.split(';')[0]} · {bytes(asset['file:size'])}
              {asset.roles.includes('cloud-optimized') && ' · cloud-optimized'}
            </span>
          </li>
        ))}
      </ul>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
