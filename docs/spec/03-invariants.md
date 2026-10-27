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
role (after role hierarchy), group or their own subject. A principal reads an item when they
read its collection **and** their clearance covers the item's own marking, which may be higher
than its collection's (ACC-3). Applies to every list, get, search, STAC, tile and download
route, and to every signed URL the API hands out.

### ACC-2 Non-disclosure

Status: enforced

A resource the caller may not read is indistinguishable from one that does not exist:
same status code and same body. Markings can't be probed by guessing ids.

### ACC-3 Marking floor

Status: enforced

An item's marking is at least its collection's, and a derivative's marking is at least its
source's: a derivative has no marking of its own, and every path to its bytes (a read URL, a
preview, a scene) is opened only for a caller who may read its item, so it is always served at
its item's marking. The page banner shows the highest marking among everything currently displayed,
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

Map tiles are audited by the session they belong to, not one by one: a map view is hundreds of
tiles. Opening a footprint view (`POST /tiles/footprints`), a preview of one item
(`POST /items/{id}/preview`) or a scene of one (`POST /items/{id}/scene`, its 3D Tiles) writes
the event, with the filters or item it covers and the highest marking it could show when it
opened; the session lasts at most 15 minutes. A footprint view serves only its opener's requests.
Previews and scenes are served by the tiler, which has no login: their requests carry the signed
token the API handed the opener, which names the one item. A tile
request outside a live session is refused with 404 and no event: it names nothing but a session
id or a token.

A live status stream (`GET /uploads/events`) is one request, audited once when it opens, with the
highest marking among the uploads it watches then. It shows only the caller's own uploads, each
audited when the caller started it, and lasts minutes before the client opens another.

The pipeline is audited the same way: every outcome it gives a blob version writes exactly one
event, as the system actor `system:pipeline`, committed in the same transaction as the outcome.
A cataloged version is `allowed`; a quarantined or rejected one is `denied`, with the reason.

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

Every signed URL the API hands out covers exactly one blob and carries only the permissions
its purpose needs. An upload URL may create and write, never read, list or delete, and expires
within one hour. A read URL may only read, expires within 15 minutes, and makes storage serve
the blob as a download of the type the pipeline found, whatever headers the uploader set.
Checked on every issued URL, and against Azurite to prove the storage service enforces what the
URL says.

### ACC-9 Every byte path asks the same question

Status: enforced

Downloads, thumbnails, preview and scene tokens, raster tiles, 3D tiles and footprint tiles
serve only items the caller may read under `authz.readable_scopes`, and an unreadable item answers
exactly like a missing one (ACC-1 and ACC-2, extended from rows to bytes). A preview or scene token
serves only the item it was minted for, from the one blob it names, on its own kind of route. A file is served by a read URL for its own blob and no other: an item's
original, or its newest derivative in the role asked for.

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
at most one item.

Nothing is retried forever. A version still failing on its message's last delivery is
rejected as `retries-exhausted`, and one whose message is dead-lettered anyway (the worker died
on that delivery) is rejected as `dead-lettered` when the worker sweeps the dead-letter queue.
Its upload is closed as rejected unless a newer version has replaced it. Since outcomes are
final, an operator cannot replay a swept version: the uploader uploads it again. Checked once
the simulation sweeps dead letters and drains with faults off.

### ING-3 Originals are immutable

Status: enforced

Once an item is cataloged, its raw blob is never overwritten, changed or deleted by the
pipeline; its content hash is fixed. A new upload of the same file into another collection is a
new item with its own raw blob; into the same collection it is a duplicate (ING-9). (A version
that loses to a newer one before it is cataloged may have its raw copy discarded; nothing ever
referred to it.)

### ING-4 Quarantine is sealed

Status: enforced

A blob the scanner flags never reaches `raw/` and never becomes an item, so nothing can
extract, serve, sign or return it. Checked in the pipeline simulation, and after it drains by
asking the API for every quarantined or rejected upload as an Admin: no item route or search
returns one.

### ING-5 Catalog and storage agree

Status: enforced

Every catalog item points at a raw blob whose hash matches the item's checksum, and every
raw blob has a catalog item or an in-flight job. Holds after every step of the pipeline
simulation, including those that fail midway.

### ING-6 Header-only extraction

Status: enforced

Metadata extraction fetches at most `MAX_EXTRACT_BYTES` (4 MiB) of a file, by range, whatever
the file's size or contents; a header that needs more is recorded as `over-budget`, not read.
Every format reader goes through the blob store port, so GDAL and Arrow are bound by the same
budget. Checked against a byte-counting blob store with files several times the budget, by
fuzzing every reader, and in the pipeline simulation on every cataloged item.

### ING-8 Readers and recipes can't take the worker down

Status: enforced

Format readers and derive recipes parse untrusted bytes in native code, so each extraction and
each render runs in a child process with a deadline and a memory limit. A reader that crashes,
hangs, runs out of memory or raises unexpectedly leaves its item cataloged with extraction
`failed` and no metadata; a recipe that does any of those, or writes a file larger than a
derivative may be, fails that one derivative as `unrenderable`; either way the worker goes on
to the next message. The budget (ING-6) is enforced outside the child, and so is the rule that a
render's output is a file inside its own scratch directory. A crashing child writes no core
dump: its memory holds the file's contents, which may be CUI.

### ING-9 One item per content per collection

Status: enforced

No two items in a collection have the same checksum. A version whose bytes match an item
already in its collection is rejected as `duplicate`, and its raw copy is discarded; the
database refuses a second item with the same checksum even when two such uploads race. Items
cataloged before this rule (migration 0007) are exempt.

### ING-10 A running job keeps its message

Status: enforced

While the worker processes a message, its lock is renewed (up to `queue_lock_renewal_seconds`),
so a long copy or hash is never redelivered to another worker mid-job or charged a delivery for
a lock that lapsed. Checked against the fake queue's lock model with a slow store, and against
the Service Bus emulator.

### ING-7 Derivatives are rebuildable

Status: enforced

Deleting every derivative and rerunning derive yields equivalent assets: the same roles, media
types and lineage, with equal checksums where the recipe is deterministic, ignoring timestamps.
Checked at the end of the derive simulation, which deletes every derivative and asks again.

### ING-11 Deriving follows cataloging and never changes it

Status: enforced

Every cataloged item, and nothing else, is handed to the derive stage, by the transaction that
catalogs it. A derive job only adds assets to a cataloged item: it never changes the item or its
original, and never runs on a version that was quarantined or rejected (ING-4), which has no
item. Checked by the pipeline simulation (the hand-off) and the derive simulation (the rest).

### ING-12 One asset per role and recipe

Status: enforced

However often a derive job is redelivered or interrupted, it reaches one outcome, `derived` or
`failed`, which never changes; an item ends with at most one asset per `(role,
recipe_version)`; every asset row and its blob in `derived/` agree; and every blob there has an
asset or an unfinished job (ING-1, ING-2 and ING-5, extended to derivatives). Holds after every
step of the derive simulation, including those that fail midway.

### ING-13 Ready means derived

Status: enforced

An item is marked settled, and its upload reports `ready`, only while every derivative that applied
to it when its derivatives were last asked for has an outcome: none is in flight, however often
its request is redelivered or a derive stage dies midway. A recipe version deployed since is made
the next time the item is asked for (a rebuild asks for every item). Once the derive queue drains, every item is settled, so no upload reports
`deriving` for ever. Holds after every step of the derive simulation, and after it drains.
The item says the same (`ingest:derivatives`), and lists as failed (`ingest:derivative_failures`)
only derivatives it lacks: a role whose newest recipe failed but an older one made is not missing.

### ING-14 Tiles are faithful

Status: enforced

A mesh's 3D Tiles lose nothing and claim no more than they are: the leaves hold every triangle of
the mesh once (none lost, none repeated, vertices as exact as float32 allows); every tile has a
bounding volume holding its content and its children's volumes; geometric error falls strictly
from each tile to its children, down to zero at the leaves; and every vertex of a coarser tile
lies within that tile's geometric error of a vertex of the mesh. Checked over random models by
the tiler's property tests, and by Cesium's 3d-tiles-validator over a textured one.

## Lifecycle

### LIF-1 Legal hold wins

Status: planned (M5)

Nothing under legal hold is deleted, or tiered towards deletion, whatever its retention
policy says.

## Search and readings

### SRCH-1 Filters are honest

Status: enforced

Every result of a search matches every filter it was given: its footprint intersects the
query's `bbox` or `intersects` geometry (boundaries touching counts), its time (when it was
captured, or when it was cataloged if that isn't known) overlaps the `datetime` interval, its
cataloging time is in the `created` interval, each `q` term appears in its filename or a tag, and its collection, kind and
id are among those asked for. Checked by a simulation that compares each page against an
independent model of the filters.

### SRCH-2 Pagination is complete

Status: enforced

Paging through a search with its `next` tokens returns every item that matched when paging
began exactly once, in a fixed order, however many items are cataloged between pages (those
appear at most once), and once they stop arriving the walk ends: the last page says there is
no next. Checked with items that share a
timestamp and items cataloged mid-search.

### RDG-1 Compaction conserves readings

Status: planned (M5)

Compacting hot readings to Parquet changes neither the row count nor per-device sums.

## Robustness

### ROB-1 No server errors

Status: enforced

No request, however malformed, produces a 5xx. Checked by schema-driven fuzzing of every
route as several users.
