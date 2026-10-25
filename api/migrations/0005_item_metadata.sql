-- Header metadata on catalog items (M1 slice 4, ING-6).

ALTER TABLE catalog.items
    -- How extraction went; see `ingest_api.items.metadata.Status`. Items cataloged before this
    -- migration were never read, and say so as `unsupported`.
    ADD COLUMN extraction      text NOT NULL DEFAULT 'unsupported'
        CHECK (extraction IN ('extracted', 'unsupported', 'unreadable', 'over-budget')),
    ADD COLUMN extracted_bytes bigint NOT NULL DEFAULT 0 CHECK (extracted_bytes >= 0),
    -- The native CRS, `AUTHORITY:code` or WKT2.
    ADD COLUMN crs             text,
    -- WGS84 footprint; null for data that isn't placed on the earth.
    ADD COLUMN footprint       geometry(Polygon, 4326),
    -- STAC-style properties: `proj:`, `raster:`, `pc:`, `table:`, and our own `mesh:`.
    ADD COLUMN properties      jsonb NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(properties) = 'object'),
    -- Only extracted items carry metadata.
    ADD CONSTRAINT items_metadata_needs_extraction CHECK (
        extraction = 'extracted' OR (crs IS NULL AND footprint IS NULL AND properties = '{}')
    );

-- From now on the pipeline always says how extraction went.
ALTER TABLE catalog.items ALTER COLUMN extraction DROP DEFAULT;
ALTER TABLE catalog.items ALTER COLUMN extracted_bytes DROP DEFAULT;

CREATE INDEX items_footprint ON catalog.items USING gist (footprint);
