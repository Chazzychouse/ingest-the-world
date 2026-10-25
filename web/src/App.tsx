import { useState } from 'react'
import { useAuth } from 'react-oidc-context'
import { useApi, type Collection, type Marking, type Me, type StacItem } from './api'
import { bannerFor, markingText } from './markings'
import { Search } from './Search'
import { Upload } from './Upload'
import { useUploads } from './useUploads'

function MarkingBanner({ marking, position }: { marking: Marking; position: 'top' | 'bottom' }) {
  return (
    <div className={`banner banner-${position}`} style={{ background: marking.banner_color }} role="note">
      {marking.label}
    </div>
  )
}

function SignIn() {
  const auth = useAuth()
  return (
    <main className="signin">
      <h1>Ingest the World</h1>
      <p>Geospatial data ingestion, cataloging and search.</p>
      <button onClick={() => auth.signinRedirect({ state: window.location.search })} disabled={auth.isLoading}>
        Sign in with your organization
      </button>
      {auth.error && <p className="error">Sign-in failed: {auth.error.message}</p>}
    </main>
  )
}

function Identity({ me }: { me: Me }) {
  return (
    <section className="panel">
      <h2>Signed in</h2>
      <dl>
        <dt>User</dt>
        <dd>{me.name ?? me.username}</dd>
        <dt>Roles</dt>
        <dd>{me.roles.filter((r) => !r.startsWith('default-roles')).join(', ') || 'none'}</dd>
        <dt>Clearance</dt>
        <dd>
          <span className="chip" style={{ background: me.clearance.banner_color }}>
            {me.clearance.label}
          </span>
        </dd>
      </dl>
    </section>
  )
}

function Collections({ collections }: { collections: Collection[] }) {
  if (collections.length === 0) {
    return (
      <section className="panel">
        <h2>Collections</h2>
        <p className="muted">No collections are shared with you yet.</p>
      </section>
    )
  }
  return (
    <section className="panel">
      <h2>Collections</h2>
      <table>
        <thead>
          <tr>
            <th>Marking</th>
            <th>Collection</th>
            <th>Retention</th>
          </tr>
        </thead>
        <tbody>
          {collections.map((c) => (
            <tr key={c.id}>
              <td>
                <span className="chip" style={{ background: c.marking_color }}>
                  {markingText(c)}
                </span>
              </td>
              <td>
                <div className="title">{c.title}</div>
                <code>{c.id}</code>
                <p className="muted">{c.description}</p>
              </td>
              <td>
                {c.retention.retain_days ? `${c.retention.retain_days} days` : 'indefinite'}
                {c.retention.legal_hold && <div className="hold">legal hold</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

type Tab = 'search' | 'upload' | 'collections'
const TABS: Record<Tab, string> = { search: 'Search', upload: 'Upload', collections: 'Collections' }

function Workspace() {
  const auth = useAuth()
  const me = useApi<Me>('/me')
  const collections = useApi<Collection[]>('/collections')
  const [tab, setTab] = useState<Tab>('search')
  const [shown, setShown] = useState<StacItem[]>([])
  const { uploads, entries } = useUploads()
  const active = entries.filter((e) => ['queued', 'uploading', 'processing'].includes(e.phase)).length

  // Search reads its state from the URL when it opens, so an item opens by link.
  const openItem = (id: string) => {
    window.history.replaceState(window.history.state, '', `?item=${encodeURIComponent(id)}`)
    setTab('search')
  }

  // The highest marking on screen: the collections listed, and on the search tab its results.
  const banner = bannerFor(collections.data ?? [], tab === 'search' ? shown : [])

  return (
    <>
      <MarkingBanner marking={banner} position="top" />
      <header className="topbar">
        <span className="brand">Ingest the World</span>
        <nav className="tabs">
          {(Object.keys(TABS) as Tab[]).map((t) => (
            <button key={t} className={`link${tab === t ? ' active' : ''}`} onClick={() => setTab(t)}>
              {TABS[t]}
              {t === 'upload' && active > 0 && <span className="count">{active}</span>}
            </button>
          ))}
        </nav>
        <button className="link" onClick={() => auth.signoutRedirect()}>
          Sign out
        </button>
      </header>
      {collections.error && <p className="error">Couldn't load collections: {collections.error.message}</p>}
      {tab === 'search' && <Search collections={collections.data ?? []} onShown={setShown} />}
      {tab === 'upload' && (
        <Upload collections={collections.data ?? []} uploads={uploads} entries={entries} onOpenItem={openItem} />
      )}
      {tab === 'collections' && (
        <main className="workspace">
          {me.error && <p className="error">Couldn't load your profile: {me.error.message}</p>}
          {me.data && <Identity me={me.data} />}
          {collections.data && <Collections collections={collections.data} />}
        </main>
      )}
      <MarkingBanner marking={banner} position="bottom" />
    </>
  )
}

export default function App() {
  const auth = useAuth()
  if (auth.isLoading && !auth.isAuthenticated) return <main className="signin">Loading…</main>
  return auth.isAuthenticated ? <Workspace /> : <SignIn />
}
