import { StorageError, type Storage } from './transfer'

// The Blob Storage REST version these requests are written against.
const VERSION = '2025-01-05'

async function put(url: string, init: RequestInit): Promise<void> {
  let response: Response
  try {
    response = await fetch(url, { ...init, method: 'PUT' })
  } catch (error) {
    // An abort is the caller's doing, not a storage failure.
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new StorageError(0, `storage unreachable: ${(error as Error).message}`)
  }
  if (!response.ok) throw new StorageError(response.status, `storage answered ${response.status} ${response.statusText}`)
}

/** Blob Storage itself, through the signed URL's query string. Parameters are appended, never
 * set, so the signature stays as signed. */
export const blobStorage: Storage = {
  stage: (url, id, body, signal) =>
    put(`${url}&comp=block&blockid=${encodeURIComponent(id)}`, { body, signal, headers: { 'x-ms-version': VERSION } }),
  commit: (url, blockList, contentType, signal) =>
    put(`${url}&comp=blocklist`, {
      body: blockList,
      signal,
      headers: {
        'x-ms-version': VERSION,
        'Content-Type': 'application/xml',
        ...(contentType && { 'x-ms-blob-content-type': contentType }),
      },
    }),
}
