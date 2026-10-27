"""Plants known bugs one at a time and checks the suite catches each with the right invariant.

A green suite only means something if it goes red when the code is wrong. Each mutant here
is a realistic mistake; the run fails if any mutant survives, or is caught for the wrong
reason. Add a mutant whenever you add an invariant or fix a bug that tests missed.

    uv run python tools/mutants.py            # all mutants, each against its invariants' tests
    uv run python tools/mutants.py admin      # mutants whose name contains "admin"
    uv run python tools/mutants.py --full     # each against the whole suite (nightly)
    uv run python tools/mutants.py --jobs=4   # how many mutants run at once (default: below)

By default a mutant runs only the tests that declare one of its `caught_by` invariants
(`--invariant`), since those are the tests that must catch it; that is minutes, not most of
an hour. `--full` runs everything per mutant, which also shows a catch moving to some other
test, and is what the nightly build runs. Mutants run several at a time, each in its own copy of
the tree and its own databases.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Mutants run at once by default: each is one pytest process, mostly waiting on Postgres.
DEFAULT_JOBS = min(8, os.cpu_count() or 1)


@dataclass(frozen=True)
class Mutant:
    name: str
    file: str
    old: str
    new: str
    caught_by: tuple[str, ...]  # the run must fail at least one of these invariants


LIST_AUDIT = "    auditor.allowed(\n        AuditAction.COLLECTION_LIST,"
PUT_GRANT_CHECK = (
    "row = readable_or_404(conn, auditor, action, collection_id, principal)\n    store.put_grant("
)
ROUTES = "ingest_api/collections/routes.py"
UPLOAD_ROUTES = "ingest_api/uploads/routes.py"
UPLOAD_STORE = "ingest_api/uploads/store.py"
# The latest definition of the access functions; earlier migrations' versions are replaced.
UPLOADS_SQL = "migrations/0003_uploads.sql"
ITEMS_SQL = "migrations/0004_items_and_jobs.sql"
PIPELINE = "ingest_api/worker/pipeline.py"
ITEM_ROUTES = "ingest_api/items/routes.py"
ITEM_STORE = "ingest_api/items/store.py"
SEARCH_SQL = "migrations/0008_item_search.sql"
DERIVATIVES_SQL = "migrations/0014_derivative_status.sql"
DERIVE = "ingest_api/worker/derive.py"
SANDBOX = "ingest_api/adapters/process_sandbox.py"
BLOB_ADAPTER = "ingest_api/adapters/azure_blob.py"
TILE_ROUTES = "ingest_api/tiles/routes.py"
TILE_STORE = "ingest_api/tiles/store.py"
PREVIEWS = "ingest_api/previews.py"
TILER = "ingest_api/worker/meshes/tiler.py"
GLTF = "ingest_api/worker/meshes/gltf.py"
READ_GRANTED = (
    "      AND c.id IN (SELECT authz.granted_collections(subject, roles, groups, 'read'))"
)

MUTANTS = [
    Mutant(
        "admin-bypasses-clearance",
        UPLOADS_SQL,
        f"    WHERE authz.cleared_for(c.marking, clearances)\n{READ_GRANTED}",
        f"    WHERE (authz.cleared_for(c.marking, clearances) OR 'Admin' = ANY (roles))\n"
        f"{READ_GRANTED}",
        # Shows up as reading CUI (ACC-1) or granting on it (ACC-4), whichever runs first.
        ("ACC-1", "ACC-4"),
    ),
    Mutant(
        "group-grant-matches-roles",
        UPLOADS_SQL,
        "(g.principal_type = 'group' AND g.principal = ANY (groups))",
        "(g.principal_type = 'group' AND g.principal = ANY (roles || groups))",
        ("ACC-1",),
    ),
    Mutant(
        "no-role-hierarchy",
        "ingest_api/auth.py",
        "return self.roles | {role for role, rank in ROLE_RANK.items() if rank <= top}",
        "return self.roles",
        # Breaks reading (a grant to Reader stops covering a Curator) and writing (a Curator
        # stops counting as a Contributor); the write check often fails first.
        ("ACC-1", "ACC-7"),
    ),
    Mutant(
        "forbidden-reveals-existence",
        ROUTES,
        "status_code=status.HTTP_404_NOT_FOUND,",
        "status_code=403 if store.marking_of(conn, collection_id) else 404,",
        ("ACC-2",),
    ),
    Mutant(
        "list-not-audited",
        ROUTES,
        LIST_AUDIT,
        LIST_AUDIT.replace("auditor.allowed(", "(lambda *_, **__: None)("),
        ("ACC-5",),
    ),
    Mutant(
        "create-skips-clearance",
        ROUTES,
        "    if not cleared:",
        "    if False:",
        ("ACC-4",),
    ),
    Mutant(
        "grant-skips-readability",
        ROUTES,
        PUT_GRANT_CHECK,
        PUT_GRANT_CHECK.replace(
            "principal)",
            '__import__("dataclasses").replace(principal, clearances=frozenset({"CUI"})))',
        ),
        ("ACC-4",),
    ),
    Mutant(
        "denial-audit-omits-marking",
        ROUTES,
        "        marking=store.marking_of(conn, collection_id),",
        "        marking=None,",
        ("ACC-5",),
    ),
    Mutant(
        "nul-reaches-postgres",
        "ingest_api/collections/models.py",
        "title: str = Field(min_length=1, max_length=200, pattern=NO_NUL_PATTERN)",
        "title: str = Field(min_length=1, max_length=200)",
        ("ROB-1",),
    ),
    Mutant(
        "read-grant-allows-upload",
        UPLOADS_SQL,
        "granted_collections(subject, roles, groups, 'write')",
        "granted_collections(subject, roles, groups, 'read')",
        ("ACC-7",),
    ),
    Mutant(
        "upload-skips-role-check",
        UPLOADS_SQL,
        "      AND 'Contributor' = ANY (roles)\n",
        "",
        ("ACC-7",),
    ),
    Mutant(
        "anyone-renews-an-upload",
        UPLOAD_ROUTES,
        "    if row.created_by != principal.subject:",
        "    if False:",
        ("ACC-7",),
    ),
    Mutant(
        "upload-url-can-read",
        BLOB_ADAPTER,
        "BlobSasPermissions(create=True, write=True)",
        "BlobSasPermissions(read=True, create=True, write=True)",
        ("ACC-8",),
    ),
    Mutant(
        # Found on the running stack, not by the suite: Compose names Azurite by host, the SDK
        # then reads the account name from the host, and storage refused every signed URL.
        "sas-signed-for-sdk-parsed-account",
        BLOB_ADAPTER,
        '"account_name": account_name(account_url),',
        '"account_name": BlobServiceClient(account_url).account_name or "",',
        ("ACC-8",),
    ),
    Mutant(
        "upload-url-outlives-limit",
        UPLOAD_ROUTES,
        "return timedelta(minutes=runtime.settings.upload_url_minutes)",
        "return timedelta(days=1)",
        ("ACC-8",),
    ),
    Mutant(
        "audit-can-be-deleted",
        "migrations/0001_foundation.sql",
        "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.events",
        "BEFORE UPDATE ON audit.events",
        ("ACC-6",),
    ),
    Mutant(
        "landing-cleared-without-etag",
        PIPELINE,
        "store.delete(LANDING, event.name, if_match=event.etag)",
        "store.delete(LANDING, event.name)",
        # Finishing an old version deletes a newer one that landed meanwhile: it is never seen.
        ("ING-2",),
    ),
    Mutant(
        "redelivery-decides-again",
        PIPELINE,
        'outcome = job.stage if job.stage != "received" else decide(conn, p, event, upload)',
        "outcome = decide(conn, p, event, upload)",
        # The duplicate finds its upload closed, "rejects" itself and discards the item's raw.
        ("ING-1", "ING-3", "ING-5"),
    ),
    Mutant(
        "scan-ignored",
        PIPELINE,
        "    if not verdict.clean:\n",
        "    if verdict.clean and not verdict.clean:\n",
        ("ING-4",),
    ),
    Mutant(
        "checksum-not-verified",
        PIPELINE,
        "if upload.sha256 is not None and sha256 != upload.sha256:",
        "if False:",
        ("ING-2",),
    ),
    Mutant(
        "rejection-keeps-raw-copy",
        PIPELINE,
        "    if raw is not None and raw.metadata.get(SOURCE_ETAG) == etag:\n",
        "    if False:\n",
        ("ING-5",),
    ),
    Mutant(
        "promote-before-scan",
        PIPELINE,
        "    verdict = p.scanner.scan(LANDING, event.name)\n",
        "    _copy_to_raw(p.store, event)\n    verdict = p.scanner.scan(LANDING, event.name)\n",
        ("ING-4", "ING-5"),
    ),
    Mutant(
        "job-outcome-can-change",
        ITEMS_SQL,
        "    IF OLD.stage <> 'received' AND NEW.stage IS DISTINCT FROM OLD.stage THEN",
        "    IF false THEN",
        ("ING-2",),
    ),
    Mutant(
        "item-marking-floor-dropped",
        ITEMS_SQL,
        "    IF (SELECT rank FROM authz.markings WHERE code = NEW.marking) < (",
        "    IF false AND (SELECT rank FROM authz.markings WHERE code = NEW.marking) < (",
        ("ACC-3",),
    ),
    Mutant(
        "sandbox-writes-core-dumps",
        SANDBOX,
        "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n",
        "",
        ("ING-8",),
    ),
    Mutant(
        # Slice 3-4 review: a reader that crashed natively took the worker down on every
        # delivery, and its version never reached an outcome.
        "reader-crash-escapes-sandbox",
        SANDBOX,
        "            return EXITED\n",
        '            raise RuntimeError("the extraction child died")\n',
        ("ING-8",),
    ),
    Mutant(
        "last-delivery-not-given-up",
        "ingest_api/worker/runner.py",
        "        if message.delivery_count >= retry.max_deliveries:\n",
        "        if False:\n",
        ("ING-2",),
    ),
    Mutant(
        "dead-letters-dropped-by-sweep",
        "ingest_api/worker/runner.py",
        "    return _give_up(connect, _ingest(pipeline, event, Reason.DEAD_LETTERED))\n",
        "    return Settlement.COMPLETE\n",
        ("ING-2",),
    ),
    Mutant(
        "message-locks-not-renewed",
        "ingest_api/adapters/service_bus.py",
        "        if self._renewal > 0:\n",
        "        if False:\n",
        ("ING-10",),
    ),
    Mutant(
        "pipeline-outcome-not-audited",
        PIPELINE,
        "    if not audit:\n",
        "    if True:\n",
        ("ACC-5",),
    ),
    Mutant(
        "duplicates-not-rejected",
        "migrations/0007_dedup.sql",
        "CREATE UNIQUE INDEX items_one_per_content ON catalog.items (collection_id, sha256)",
        "CREATE INDEX items_one_per_content ON catalog.items (collection_id, sha256)",
        ("ING-9",),
    ),
    Mutant(
        # Found by the sample corpus: a COG cut short kept its header, so it was cataloged as
        # extracted, with metadata for pixels that aren't there.
        "truncated-tiff-extracted",
        "ingest_api/worker/formats/raster.py",
        "            _check_complete(dataset, source.size)\n",
        "",
        ("ING-6",),
    ),
    Mutant(
        # Found by the sample corpus: every 3DEP and COPC tile's CRS was stored as pages of WKT.
        "compound-crs-as-wkt",
        "ingest_api/worker/formats/crs.py",
        "    return _compound_code(crs, wkt) or wkt\n",
        "    return wkt\n",
        ("ING-6",),
    ),
    Mutant(
        "extraction-ignores-budget",
        "ingest_api/worker/extract.py",
        "        if self.fetched + length > self.limit:",
        "        if False and self.fetched + length > self.limit:",
        ("ING-6",),
    ),
    Mutant(
        # An item marked above its collection is shown to anyone who can read the collection:
        # listed by a search (ACC-1), or previewed or downloaded (ACC-9), whichever runs first.
        "item-marking-ignored",
        SEARCH_SQL,
        "    WHERE authz.cleared_for(m.code, clearances)\n",
        "    WHERE true\n",
        ("ACC-1", "ACC-9"),
    ),
    Mutant(
        "unreadable-item-is-forbidden",
        ITEM_ROUTES,
        "        status_code=status.HTTP_404_NOT_FOUND,\n        message=NOT_FOUND,",
        "        status_code=status.HTTP_403_FORBIDDEN if store.marking_of(conn, item_id)\n"
        "        else status.HTTP_404_NOT_FOUND,\n        message=NOT_FOUND,",
        # Items and their files share the refusal; whichever is asked first reports it.
        ("ACC-2", "ACC-9"),
    ),
    Mutant(
        "search-unaudited",
        ITEM_ROUTES,
        "    auditor.allowed(\n        AuditAction.ITEM_SEARCH,",
        "    (lambda *_, **__: None)(\n        AuditAction.ITEM_SEARCH,",
        ("ACC-5",),
    ),
    Mutant(
        # Items sharing a timestamp with the end of a page are skipped.
        "cursor-without-tiebreak",
        ITEM_STORE,
        '"(i.created_at, i.id) < (%s, %s)"',
        '"(i.created_at < %s OR %s IS NULL)"',
        ("SRCH-2",),
    ),
    Mutant(
        "cursor-repeats-last-item",
        ITEM_STORE,
        '"(i.created_at, i.id) < (%s, %s)"',
        '"(i.created_at, i.id) <= (%s, %s)"',
        ("SRCH-2",),
    ),
    Mutant(
        "bbox-wraps-the-wrong-way",
        ITEM_STORE,
        "    if west <= east:\n        return [bbox]",
        "    if True:\n        return [bbox]",
        # Wrapping the wrong way both returns items off the query and misses ones on it.
        ("SRCH-1", "SRCH-2"),
    ),
    Mutant(
        "search-terms-unescaped",
        ITEM_STORE,
        '    return f"%{escaped}%"',
        '    return f"%{term}%"',
        ("SRCH-1",),
    ),
    Mutant(
        "upload-list-shows-everyones",
        UPLOAD_STORE,
        'query = _SELECT_READABLE + " AND u.created_by = %s"',
        'query = _SELECT_READABLE + " AND %s IS NOT NULL"',
        ("ACC-1",),
    ),
    Mutant(
        "upload-list-repeats-page-edge",
        UPLOAD_STORE,
        'query += " AND (u.created_at, u.id) < (%s, %s)"',
        'query += " AND (u.created_at, u.id) <= (%s, %s)"',
        ("ACC-1",),
    ),
    Mutant(
        "datetime-filters-upload-time",
        ITEM_STORE,
        "overlaps = sql.SQL(\"i.datetime_range && tstzrange(%s, %s, '[]')\")",
        "overlaps = sql.SQL(\"tstzrange(i.created_at, i.created_at, '[]')\"\n"
        "    \" && tstzrange(%s, %s, '[]')\")",
        # Returns items off the filter (SRCH-1) and misses ones on it (SRCH-2).
        ("SRCH-1", "SRCH-2"),
    ),
    Mutant(
        # Two versions of a recipe share a blob: the newer claims the older's derivative.
        "derivative-name-ignores-version",
        DERIVE,
        "/{recipe.role}/v{recipe.version}{recipe.suffix}",
        "/{recipe.role}{recipe.suffix}",
        ("ING-12",),
    ),
    Mutant(
        # Looks the original up by upload, so a request naming a rejected upload is derived.
        "derive-reads-uploads-not-items",
        ITEM_STORE,
        '            "SELECT id, collection_id, kind, media_type, raw_blob, size_bytes, sha256, "\n'
        '            "extraction, crs, properties FROM catalog.items WHERE id = %s",',
        "            \"SELECT u.id, u.collection_id, coalesce(i.kind, 'generic') AS kind, \"\n"
        "            \"coalesce(i.media_type, '') AS media_type, u.blob_name AS raw_blob, \"\n"
        "            \"u.size_bytes, coalesce(i.sha256, u.sha256, '') AS sha256, \"\n"
        "            \"coalesce(i.extraction, 'unsupported') AS extraction, i.crs, \"\n"
        "            \"coalesce(i.properties, '{}') AS properties \"\n"
        '            "FROM ingest.uploads u LEFT JOIN catalog.items i ON i.id = u.id "\n'
        '            "WHERE u.id = %s",',
        ("ING-11",),
    ),
    Mutant(
        # Found by the derive simulation: a write whose acknowledgement was lost, then retries
        # running out, left a blob in derived/ that no asset or job accounted for.
        "give-up-orphans-a-written-derivative",
        DERIVE,
        "written = d.store.head(DERIVED, blob_name(source.collection_id, source.item_id, recipe))",
        "written = None",
        ("ING-12",),
    ),
    Mutant(
        # Records the asset whether or not its blob was written.
        "asset-recorded-without-its-blob",
        DERIVE,
        '    try:\n        with path.open("rb") as data:\n            return d.store.create(',
        "    written = BlobProps(DERIVED, name, size, '', recipe.media_type, metadata)\n"
        "    try:\n"
        '        with path.open("rb") as data:\n'
        "            d.store.create(DERIVED, name, data, metadata=metadata)\n"
        "    except StorageError:\n"
        "        pass\n"
        "    return written\n"
        "    try:\n"
        '        with path.open("rb") as data:\n'
        "            return d.store.create(",
        ("ING-12",),
    ),
    Mutant(
        # Takes a sandboxed render's word for where its output is, so a compromised one can
        # publish any file the worker can read as a derivative.
        "render-output-trusted",
        DERIVE,
        "    if not (path.resolve().is_relative_to(scratch.resolve()) and path.is_file()):\n",
        "    if not path.is_file():\n",
        ("ING-12",),
    ),
    Mutant(
        "render-writes-without-limit",
        SANDBOX,
        "    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))\n",
        "",
        ("ING-8",),
    ),
    Mutant(
        # Renders whatever is in raw/ now, while lineage names the checksum cataloged.
        "derive-trusts-the-original-unchecked",
        DERIVE,
        "    if digest.hexdigest() != source.sha256:\n",
        "    if False:\n",
        ("ING-7",),
    ),
    Mutant(
        # Finds the derivative by role alone: any item's thumbnail answers for this one's.
        "asset-of-another-item",
        ITEM_STORE,
        '" WHERE item_id = %s AND role = %s"',
        '" WHERE (item_id = %s OR true) AND role = %s"',
        ("ACC-9",),
    ),
    Mutant(
        # A URL for a blob other than the one asked for: reported as ACC-8 by the URL check.
        "asset-served-at-its-oldest-version",
        ITEM_STORE,
        '_NEWEST_FIRST = " ORDER BY item_id, role, recipe_version DESC"',
        '_NEWEST_FIRST = " ORDER BY item_id, role, recipe_version"',
        ("ACC-8", "ACC-9"),
    ),
    Mutant(
        "read-url-grants-write",
        BLOB_ADAPTER,
        "READ_PERMISSION = BlobSasPermissions(read=True)",
        "READ_PERMISSION = BlobSasPermissions(read=True, write=True)",
        ("ACC-8",),
    ),
    Mutant(
        # Copied from the upload route: a read URL living an hour.
        "read-url-lives-as-long-as-an-upload",
        ITEM_ROUTES,
        "minutes=runtime.settings.read_url_minutes",
        "minutes=runtime.settings.upload_url_minutes",
        ("ACC-8",),
    ),
    Mutant(
        # Serves the blob with the headers its uploader set: an HTML "image" renders on
        # storage's origin.
        "read-url-serves-the-uploaders-type",
        BLOB_ADAPTER,
        "return self._sign((container, name), READ_PERMISSION, expires_at, served_as)",
        "return self._sign((container, name), READ_PERMISSION, expires_at)",
        ("ACC-8",),
    ),
    Mutant(
        # Draws everything in the tile: an OR where the tile's clause joins the search's.
        "footprint-tile-ors-its-area",
        ITEM_STORE,
        "        AND i.footprint && (SELECT ST_Transform(envelope, 4326) FROM tile)",
        "        OR i.footprint && (SELECT ST_Transform(envelope, 4326) FROM tile)",
        ("ACC-9", "SRCH-1"),
    ),
    Mutant(
        "footprint-view-serves-anyone",
        TILE_STORE,
        "WHERE id = %s AND subject = %s AND expires_at > %s",
        "WHERE id = %s AND (subject = %s OR true) AND expires_at > %s",
        ("ACC-9",),
    ),
    Mutant(
        "footprint-view-never-expires",
        TILE_STORE,
        "WHERE id = %s AND subject = %s AND expires_at > %s",
        "WHERE id = %s AND subject = %s AND (expires_at > %s OR true)",
        ("ACC-9",),
    ),
    Mutant(
        "footprint-view-unaudited",
        TILE_ROUTES,
        "    auditor.allowed(\n        AuditAction.FOOTPRINTS_VIEW,",
        "    (lambda *_, **__: None)(\n        AuditAction.FOOTPRINTS_VIEW,",
        ("ACC-5",),
    ),
    Mutant(
        # Audits a view by the lowest marking it can show, not the highest.
        "footprint-view-audited-low",
        ITEM_STORE,
        '" ORDER BY m.rank DESC LIMIT 1"',
        '" ORDER BY m.rank LIMIT 1"',
        ("ACC-5",),
    ),
    Mutant(
        "preview-token-serves-any-item",
        PREVIEWS,
        'if claims["sub"] != str(item_id) or container',
        "if container",
        ("ACC-9",),
    ),
    Mutant(
        "preview-token-never-expires",
        PREVIEWS,
        "    if not isinstance(expires, int) or now.timestamp() >= expires:",
        "    if not isinstance(expires, int):",
        ("ACC-9",),
    ),
    Mutant(
        "preview-token-signature-unchecked",
        PREVIEWS,
        '"verify_exp": False}',
        '"verify_exp": False, "verify_signature": False}',
        ("ACC-9",),
    ),
    Mutant(
        # A raster that isn't a COG is previewed from its original, which a tiler can't serve
        # a tile at a time: the token names a blob other than the item's COG.
        "preview-of-a-plain-original",
        ITEM_ROUTES,
        "    if row.properties.get(CLOUD_OPTIMIZED) is True:\n"
        "        return _blob(conn, row, ORIGINAL)",
        "    if True:\n        return _blob(conn, row, ORIGINAL)",
        ("ACC-9",),
    ),
    Mutant(
        # Opening a preview or a scene goes unaudited: both share the one place that audits it.
        "preview-unaudited",
        ITEM_ROUTES,
        "blob.name, expires_at)\n    auditor.allowed(",
        "blob.name, expires_at)\n    (lambda *_, **__: None)(",
        ("ACC-5",),
    ),
    Mutant(
        "preview-outlives-its-session",
        ITEM_ROUTES,
        "dt.timedelta(minutes=runtime.settings.map_session_minutes)",
        "dt.timedelta(minutes=60)",
        ("ACC-5",),
    ),
    Mutant(
        "upload-watch-unaudited",
        UPLOAD_ROUTES,
        "Auditor(conn, ctx, Actor.of(principal)).allowed(",
        "(lambda *_, **__: None)(",
        ("ACC-5",),
    ),
    Mutant(
        # Audits a stream by its newest upload's marking, not the highest it shows.
        "upload-watch-audited-low",
        UPLOAD_ROUTES,
        "marking=_highest(watched),",
        "marking=watched[0].marking if watched else None,",
        ("ACC-5",),
    ),
    Mutant(
        "settled-before-derived",
        DERIVE,
        "        _unsettle(conn, item_id)\n        original = cache(",
        "        _settle(conn, d, item_id)\n        original = cache(",
        ("ING-13",),
    ),
    Mutant(
        # A redelivered request derives an item that still says it is ready.
        "settled-never-cleared",
        DERIVE,
        "        _unsettle(conn, item_id)\n        original = cache(",
        "        original = cache(",
        ("ING-13",),
    ),
    Mutant(
        "cataloged-is-always-ready",
        DERIVATIVES_SQL,
        "THEN 'ready' ELSE 'deriving' END",
        "THEN 'ready' ELSE 'ready' END",
        ("ING-13",),
    ),
    Mutant(
        # A derivative an older recipe made is reported missing when a newer version fails.
        "failure-despite-asset",
        DERIVATIVES_SQL,
        "    WHERE newest.stage = 'failed'\n      AND NOT EXISTS (",
        "    WHERE newest.stage = 'failed'\n      AND true OR false AND NOT EXISTS (",
        ("ING-13",),
    ),
    Mutant(
        # A footprint smaller than a pixel drops out of the tile, so it vanishes at low zoom.
        "tiny-footprint-dropped",
        ITEM_STORE,
        "ST_AsMVTGeom(ST_PointOnSurface(shape),",
        "ST_AsMVTGeom(NULL::geometry,",
        ("SRCH-1",),
    ),
    Mutant(
        # The scene routes take any token the tiler signed: a preview's opens a scene.
        "scene-takes-a-preview-token",
        "ingest_api/tiler/routes.py",
        "token, runtime.key, item_id=item_id, now=now, audience=previews.SCENE_AUDIENCE",
        "token, runtime.key, item_id=item_id, now=now",
        ("ACC-9",),
    ),
    Mutant(
        # A scene opens for an item whatever its derivatives: the token names a blob that isn't
        # its tileset (here its original), which the tiler would serve as one.
        "scene-of-the-original",
        ITEM_ROUTES,
        "    return _blob(conn, row, TILESET_ROLE)",
        "    return _blob(conn, row, ORIGINAL)",
        ("ACC-9",),
    ),
    Mutant(
        # A role the catalog's rule refuses (it must start with a letter): every mesh's tileset
        # fails to record. Found before it shipped, by the first test that recorded one.
        "tileset-role-the-catalog-refuses",
        "ingest_api/items/metadata.py",
        'TILESET_ROLE = "tileset"',
        'TILESET_ROLE = "3dtiles"',
        ("ING-12",),
    ),
    Mutant(
        # Normals written as clustering left them: opposite ones averaged to zero, which glTF
        # forbids (found by the corpus's chess set).
        "zero-normals-written",
        "ingest_api/worker/meshes/writer.py",
        'attributes["NORMAL"] = doc.accessor(_unit(part.normals), target=ARRAY_BUFFER)',
        'attributes["NORMAL"] = doc.accessor(part.normals, target=ARRAY_BUFFER)',
        ("ING-14",),
    ),
    Mutant(
        # A coarse tile declares only its own clustering's error, not what its children's had
        # already moved: a viewer trusting it shows a surface further off than it claims.
        "tile-error-ignores-children",
        TILER,
        "return clustering.cluster(merged, box[0], cell), clustering.diagonal(cell) + worst",
        "return clustering.cluster(merged, box[0], cell), clustering.diagonal(cell)",
        ("ING-14",),
    ),
    Mutant(
        # The last chunk of a primitive's triangles, short of a whole one, is never read.
        "partial-chunk-dropped",
        GLTF,
        "corners = max(0, min(count, primitive.triangles - start)) * 3",
        "corners = count * 3 if primitive.triangles - start >= count else 0",
        ("ING-14",),
    ),
    Mutant(
        # Indices are trusted: one past the vertices reads whatever the gather finds there.
        "mesh-index-unchecked",
        GLTF,
        "        if len(ids) and int(ids.max()) >= primitive.positions.count:",
        "        if False:",
        ("ING-8",),
    ),
    Mutant(
        # A tile's box is its own content's alone: a parent's no longer holds its children.
        "tile-box-without-children",
        TILER,
        "box = _union([own, *(c.box for c in children)])",
        "box = _union([own])",
        ("ING-14",),
    ),
    Mutant(
        # A tile on the antimeridian, half a cell past it, loses its footprint and its place on
        # the map: found by the demo's Copernicus tile of Taveuni.
        "edge-overshoot-refused",
        "ingest_api/items/metadata.py",
        "        west, east = _at_edge(west, -180), _at_edge(east, 180)\n",
        "",
        ("ING-6",),
    ),
]


# Copied into each mutant's sandbox; everything else is rebuildable or irrelevant to tests.
SKIP = shutil.ignore_patterns(
    ".git",
    ".venv",
    "node_modules",
    "dist",
    ".hypothesis",
    "reports",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".cache",  # samples/.cache: the downloaded corpus, gigabytes no test reads
)


def run(mutant: Mutant, snapshot: Path, *, full: bool) -> tuple[bool, str]:
    """Applies `mutant` to a throwaway copy of `snapshot` and runs the suite there: the tests
    declaring its invariants, or with `full` all of them."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "repo"
        shutil.copytree(snapshot, sandbox)
        path = sandbox / "api" / mutant.file
        original = path.read_text()
        if original.count(mutant.old) != 1:
            return False, f"pattern not found exactly once in {mutant.file} (update the mutant)"
        path.write_text(original.replace(mutant.old, mutant.new))
        report = Path(tmp) / "report.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                "-p",
                "no:cacheprovider",
                f"--invariants-report={report}",
                *([] if full else [f"--invariant={i}" for i in mutant.caught_by]),
            ],
            cwd=sandbox / "api",
            capture_output=True,
            check=False,
            env={**os.environ, "INGEST_HYPOTHESIS_PROFILE": "mutants"},
        )
        if not report.exists():
            return False, "pytest produced no report (collection error?)"
        failures = json.loads(report.read_text())["failures"]
    if not failures:
        return False, "SURVIVED: the suite passed with the bug in place"
    broken = {i for f in failures for i in f["invariants"]}
    hit = sorted(broken & set(mutant.caught_by))
    if not hit:
        expected = "/".join(mutant.caught_by)
        return False, f"caught, but as {sorted(broken) or 'no invariant'}, not {expected}"
    return True, f"caught by {', '.join(hit)} in {failures[0]['test']}"


def main(argv: list[str]) -> int:
    full = "--full" in argv
    jobs = next((int(a.split("=", 1)[1]) for a in argv if a.startswith("--jobs=")), DEFAULT_JOBS)
    names = [a for a in argv if not a.startswith("--")]
    selected = [m for m in MUTANTS if not names or any(a in m.name for a in names)]
    survived = 0
    # Every mutant runs against the tree as it was at the start, so editing code while
    # mutants run neither breaks the run nor leaks half-made changes into it.
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=jobs) as pool:
        snapshot = Path(tmp) / "repo"
        shutil.copytree(REPO, snapshot, ignore=SKIP)
        results = pool.map(partial(run, snapshot=snapshot, full=full), selected)
        for mutant, (ok, message) in zip(selected, results, strict=True):  # in the list's order
            survived += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {mutant.name:<30} {message}", flush=True)
    print(f"\n{len(selected) - survived}/{len(selected)} mutants caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
