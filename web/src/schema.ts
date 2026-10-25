import openapi from './generated/openapi.json'
import type { Kind } from './api'

// Runtime values the API's schema already states, read from it rather than retyped, so the
// browser checks input against the same limits the API enforces.
const schemas = openapi.components.schemas
const search = schemas.SearchIn.properties

/** The `anyOf` member of an optional field that isn't `null`. */
function present<T extends object>(field: { anyOf: (T | { type: string })[] }): T {
  const value = field.anyOf.find((member) => !('type' in member && member.type === 'null'))
  if (!value) throw new Error('an optional field with no type besides null')
  return value as T
}

// The generated types and this list come from the same schema.
export const KINDS = schemas.Kind.enum as Kind[]
export const MAX_QUERY_LENGTH = present<{ maxLength: number }>(search.q).maxLength
export const MAX_FILTER_VALUES = present<{ maxItems: number }>(search.collections).maxItems
export const COLLECTION_ID = new RegExp(present<{ items: { pattern: string } }>(search.collections).items.pattern)

const upload = schemas.UploadIn.properties
export const FILENAME = new RegExp(upload.filename.pattern)
export const MAX_FILENAME_LENGTH = upload.filename.maxLength
export const MAX_UPLOAD_BYTES = upload.size_bytes.maximum
export const MEDIA_TYPE = new RegExp(present<{ pattern: string }>(upload.content_type).pattern)
export const MAX_TAGS = upload.tags.maxItems
export const MAX_TAG_LENGTH = upload.tags.items.maxLength
