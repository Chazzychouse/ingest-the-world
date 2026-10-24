# Invariants

The properties the platform must hold no matter what users, networks or workers do. Each
has a stable ID; tests, simulation checks and (from M4) the production reconciler refer to
these IDs, and the test harness fails if an `enforced` invariant has no check or a check
cites an ID that isn't defined here.

Rules for changing this file:

- An invariant is weakened or removed only by editing it here, with the reason in the
  commit message. Never relax a check in code to make a test pass.
- New features list the invariants they touch in their PR, and add new ones here first.
- `Status` is `enforced` (checked in CI today) or `planned` (named so designs respect it;
  the milestone that adds its check is given).

The format below is parsed by `api/tests/invariants.py`: each invariant is a level-3
heading `### <ID> <title>` followed by a `Status:` line.

## Access and markings

### ACC-1 No leak

Status: enforced

A response only ever contains resources the caller may read. "May read" is defined by the
reference model in `api/tests/model.py`, which is written independently of
`authz.readable_collections` and checked against it: a principal reads a collection when
their clearance covers its marking **and** they are an Admin or hold a grant through a
role (after role hierarchy), group or their own subject. Applies to every list, get,
search, STAC, tile and download route, and to every signed URL the API hands out.

### ACC-2 Non-disclosure

Status: enforced

A resource the caller may not read is indistinguishable from one that does not exist:
same status code and same body. Markings can't be probed by guessing ids.

### ACC-3 Marking floor

Status: enforced (collections, items, banner); planned for derivatives (M3)

An item's marking is at least its collection's, and a derivative's marking is at least its
source's. The page banner shows the highest marking among everything currently displayed,
computed from marking rank rather than from response order.

### ACC-4 No creation above clearance

Status: enforced

Nobody creates, relabels or grants access to a resource whose marking exceeds their own
clearance. Admin role does not lift this.

### ACC-5 Audit coverage

Status: enforced

Every authenticated request to a catalog or upload route that the API accepts or refuses on
policy grounds (2xx, 403, 404, 409) writes exactly one audit event, committed before the
response is sent. Its outcome matches the response, and its `marking` is the highest marking
the request disclosed or, for a refusal on an existing target, that target's marking.
Malformed requests (401, 422) carry no identity or intent worth auditing and are left to
request logs. Health, public config and `/me` are not catalog data and are exempt.

### ACC-6 Audit is append-only

Status: enforced

No role, including the table owner, can update, delete or truncate audit events.

### ACC-7 Write needs a write grant

Status: enforced

A principal may upload into a collection only when they can read it (ACC-1), hold the
Contributor role or higher, and, unless they are an Admin, hold a `write` or `curate` grant
on it. A `read` grant never allows writing, whatever the principal's role. Only an upload's
creator, while still holding write access, may obtain further signed URLs for it. Refusals
follow ACC-2: a collection the caller can't read answers 404, one they can read but not
write, 403.

### ACC-8 Signed URLs are least-privilege

Status: enforced

Every signed URL the API hands out covers exactly one blob, carries only the permissions
its purpose needs (an upload URL may create and write, never read, list or delete), and
expires within one hour. Checked on every issued URL, and against Azurite to prove the
storage service enforces what the URL says.

## Ingest integrity

### ING-1 Idempotent processing

Status: enforced

Delivering the same blob event (blob name + eTag) one or more times, in any order and
interleaved with redeliveries after lost locks, leaves the catalog, the jobs and storage in
the same state as delivering it once. Checked by the pipeline simulation, which duplicates,
reorders and redelivers events.

### ING-2 Exactly one outcome

Status: enforced

Every blob version that lands ends in exactly one of `cataloged`, `quarantined` or `rejected`:
never lost, never cataloged twice, and an outcome once reached never changes. An upload yields
at most one item. Checked once the simulation drains with faults off.

### ING-3 Originals are immutable

Status: enforced

Once an item is cataloged, its raw blob is never overwritten, changed or deleted by the
pipeline; its content hash is fixed. A new upload of the same file is a new item with its own
raw blob. (A version that loses to a newer one before it is cataloged may have its raw copy
discarded; nothing ever referred to it.)

### ING-4 Quarantine is sealed

Status: enforced (pipeline); planned for serving and search (M1 slice 5)

A blob the scanner flags never reaches `raw/` and never becomes an item, so nothing can
extract, serve, sign or return it.

### ING-5 Catalog and storage agree

Status: enforced

Every catalog item points at a raw blob whose hash matches the item's checksum, and every
raw blob has a catalog item or an in-flight job. Holds after every step of the pipeline
simulation, including those that fail midway.

### ING-6 Header-only extraction

Status: planned (M1)

Metadata extraction for size classes M and L reads at most a bounded number of bytes
(range reads), never the whole object. Checked with a byte-counting blob store.

### ING-7 Derivatives are rebuildable

Status: planned (M3)

Deleting every derivative and rerunning the pipeline yields an equivalent catalog,
ignoring timestamps.

## Lifecycle

### LIF-1 Legal hold wins

Status: planned (M2)

Nothing under legal hold is deleted, or tiered towards deletion, whatever its retention
policy says.

## Search and readings

### SRCH-1 Filters are honest

Status: planned (M1)

Every result of a spatial search intersects the query geometry, and every result of a
facet filter matches it.

### SRCH-2 Pagination is complete

Status: planned (M1)

Paging through a search returns every match exactly once.

### RDG-1 Compaction conserves readings

Status: planned (M5)

Compacting hot readings to Parquet changes neither the row count nor per-device sums.

## Robustness

### ROB-1 No server errors

Status: enforced

No request, however malformed, produces a 5xx. Checked by schema-driven fuzzing of every
route as several users.
