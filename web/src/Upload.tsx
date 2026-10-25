import { useState, type ChangeEvent, type DragEvent } from 'react'
import type { Collection, Reason } from './api'
import { plan } from './blocks'
import { bytes } from './format'
import { markingText } from './markings'
import { MAX_TAG_LENGTH, MAX_TAGS } from './schema'
import { isLocal, parseTags, type Entry, type Phase, type Uploads } from './uploads'

const PHASES: Record<Phase, string> = {
  invalid: 'Not uploaded',
  queued: 'Waiting',
  uploading: 'Uploading',
  paused: 'Paused',
  'needs-file': 'Not finished: choose the file again to carry on',
  processing: 'Processing',
  finished: 'Done',
  failed: 'Failed',
}

const ELSEWHERE = 'Not finished uploading: choose the file to carry on'

// Why the pipeline turned a file away, in the uploader's terms.
const REASONS: Record<Reason, string> = {
  'no-upload': 'storage held no upload by this name',
  'upload-closed': 'the upload had already finished',
  superseded: 'newer bytes replaced these',
  'size-mismatch': 'the bytes stored weren’t the size declared',
  'checksum-mismatch': 'the bytes stored didn’t match the declared checksum',
  duplicate: 'this collection already has a file with the same contents',
  'retries-exhausted': 'processing kept failing; upload it again',
  'dead-lettered': 'processing couldn’t finish; upload it again',
}

/** What a processing upload is waiting on. */
function processing(entry: Entry): string {
  if (entry.stage !== 'processing') return 'In storage, waiting for the pipeline'
  const attempts = entry.attempts ?? 1
  return attempts > 1 ? `Checking and cataloging (attempt ${attempts})` : 'Checking and cataloging'
}

type FormProps = { collections: Collection[]; onFiles: (files: File[], collectionId: string, tags: string[]) => void }

function UploadForm({ collections, onFiles }: FormProps) {
  // No default: where a file goes decides its marking, so it's always chosen on purpose.
  const [collectionId, setCollectionId] = useState('')
  const [tags, setTags] = useState('')
  const [over, setOver] = useState(false)
  const chosen = collections.some((c) => c.id === collectionId) ? collectionId : ''
  const send = (files: FileList | null) => {
    if (chosen && files?.length) onFiles([...files], chosen, parseTags(tags))
  }
  const drop = (event: DragEvent) => {
    event.preventDefault()
    setOver(false)
    send(event.dataTransfer.files)
  }
  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    send(event.target.files)
    event.target.value = '' // the same file can be picked again
  }
  if (collections.length === 0) {
    return (
      <section className="panel">
        <h2>Upload</h2>
        <p className="muted">
          You can't upload into any collection yet. Uploading needs the Contributor role and a write grant on a
          collection; ask its curator.
        </p>
      </section>
    )
  }
  return (
    <section className="panel upload-form">
      <h2>Upload</h2>
      <label className="field">
        Collection
        <select value={chosen} onChange={(e) => setCollectionId(e.target.value)} required>
          <option value="" disabled>
            Choose a collection
          </option>
          {collections.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title} ({markingText(c)})
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        Tags <span className="muted">comma-separated, optional</span>
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="e.g. ortho, 2024, north site"
          maxLength={MAX_TAGS * (MAX_TAG_LENGTH + 2)}
        />
      </label>
      <label
        className={`dropzone${over && chosen ? ' over' : ''}`}
        aria-disabled={!chosen}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
      >
        <input type="file" multiple onChange={pick} className="visually-hidden" disabled={!chosen} />
        <strong>{chosen ? 'Drop files here, or choose files' : 'Choose a collection first'}</strong>
        <span className="muted">
          Any format, up to 1 TiB each. Each file takes the collection's marking, and goes straight to storage.
        </span>
      </label>
    </section>
  )
}

function Outcome({ entry, onOpen }: { entry: Entry; onOpen: (id: string) => void }) {
  if (entry.outcome === 'cataloged') {
    return (
      <span className="outcome ok">
        Cataloged
        <button className="link" onClick={() => onOpen(entry.key)}>
          Show in search
        </button>
      </span>
    )
  }
  if (entry.outcome === 'quarantined') {
    const found = entry.signature ? ` (${entry.signature})` : ''
    return <span className="outcome bad">Quarantined: the scanner found malware{found}</span>
  }
  if (entry.outcome === 'rejected') {
    return <span className="outcome bad">Rejected{entry.reason ? `: ${REASONS[entry.reason]}` : ''}</span>
  }
  return <span className="outcome">Abandoned</span>
}

type RowProps = { entry: Entry; collection: string; uploads: Uploads; onOpen: (id: string) => void }

function Row({ entry, collection, uploads, onOpen }: RowProps) {
  const { key, phase } = entry
  const sent = Math.min(entry.size, entry.stagedBlocks * plan(entry.size).size)
  const moving = phase === 'uploading' || phase === 'queued' || phase === 'paused' || (phase === 'needs-file' && !entry.elsewhere)
  return (
    <li className={`upload ${phase}`}>
      <div className="upload-head">
        <span className="title">{entry.filename}</span>
        <span className="meta">
          {bytes(entry.size)} · {collection}
        </span>
      </div>
      {moving && (
        <progress value={entry.stagedBlocks} max={entry.blocks} aria-label={`${entry.filename} uploaded`} />
      )}
      {phase === 'processing' && <progress aria-label={`${entry.filename} processing`} />}
      <div className="upload-status">
        {phase === 'finished' ? (
          <Outcome entry={entry} onOpen={onOpen} />
        ) : (
          <span>
            {phase === 'processing' ? processing(entry) : entry.elsewhere ? ELSEWHERE : PHASES[phase]}
            {moving && ` · ${bytes(sent)} of ${bytes(entry.size)}`}
            {phase === 'processing' && entry.stale && ' is taking a while'}
          </span>
        )}
        {entry.problem && <span className="error">{entry.problem}</span>}
        <span className="upload-actions">
          {phase === 'uploading' && (
            <button className="link" onClick={() => uploads.pause(key)}>
              Pause
            </button>
          )}
          {(phase === 'paused' || (phase === 'failed' && !isLocal(key))) && (
            <button className="link" onClick={() => uploads.resume(key)}>
              {phase === 'paused' ? 'Resume' : 'Try again'}
            </button>
          )}
          {phase === 'needs-file' && (
            <label className="link">
              Choose file
              <input
                type="file"
                className="visually-hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) uploads.provideFile(key, file)
                  e.target.value = ''
                }}
              />
            </label>
          )}
          {phase === 'processing' && entry.stale && (
            <button className="link" onClick={() => uploads.checkAgain(key)}>
              Check again
            </button>
          )}
          {isLocal(key) && (
            <button className="link" onClick={() => uploads.remove(key)}>
              Dismiss
            </button>
          )}
        </span>
      </div>
    </li>
  )
}

type Props = {
  collections: Collection[]
  uploads: Uploads
  entries: Entry[]
  onOpenItem: (id: string) => void
}

/** Upload files into a collection, and follow each through to the catalog. */
export function Upload({ collections, uploads, entries, onOpenItem }: Props) {
  const writable = collections.filter((c) => c.can_upload)
  const titleOf = (id: string) => collections.find((c) => c.id === id)?.title ?? id
  return (
    <main className="workspace uploads">
      <UploadForm collections={writable} onFiles={(files, id, tags) => void uploads.add(files, id, tags)} />
      <section className="panel" aria-live="polite">
        <h2>Your uploads</h2>
        {entries.length === 0 ? (
          <p className="muted">
            Nothing uploaded yet. Each file is checked, cataloged, and then searchable; you'll see how that goes here.
          </p>
        ) : (
          <ol className="upload-list">
            {entries.map((e) => (
              <Row key={e.key} entry={e} collection={titleOf(e.collectionId)} uploads={uploads} onOpen={onOpenItem} />
            ))}
          </ol>
        )}
        {uploads.hasOlder && (
          <button className="link" onClick={() => void uploads.loadOlder()}>
            Show older uploads
          </button>
        )}
      </section>
    </main>
  )
}
