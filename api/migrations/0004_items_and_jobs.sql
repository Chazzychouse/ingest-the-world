-- Catalog items and the pipeline jobs that make them (M1 slice 3).

-- What the classifier can say a file is. Kept in step with `ingest_api.items.kinds.Kind` by
-- tests/test_items.py.
CREATE TABLE catalog.kinds (code text PRIMARY KEY);
INSERT INTO catalog.kinds (code) VALUES
    ('raster'), ('vector'), ('pointcloud'), ('mesh'), ('tabular'), ('document'), ('generic');

-- One item per cataloged upload; its id is the upload's. The original lives at
-- `raw/{raw_blob}` and never changes once the item exists (ING-3).
CREATE TABLE catalog.items (
    id            uuid PRIMARY KEY REFERENCES ingest.uploads (id),
    collection_id text NOT NULL REFERENCES catalog.collections (id),
    kind          text NOT NULL REFERENCES catalog.kinds (code),
    media_type    text NOT NULL,
    filename      text NOT NULL,
    size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
    sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    raw_blob      text NOT NULL UNIQUE,
    marking       text NOT NULL REFERENCES authz.markings (code),
    tags          text[] NOT NULL DEFAULT '{}',
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL
);

CREATE INDEX items_collection ON catalog.items (collection_id, created_at DESC);

-- An item's marking is at least its collection's (ACC-3).
CREATE FUNCTION catalog.item_marking_floor() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT rank FROM authz.markings WHERE code = NEW.marking) < (
        SELECT m.rank FROM catalog.collections c JOIN authz.markings m ON m.code = c.marking
        WHERE c.id = NEW.collection_id
    ) THEN
        RAISE EXCEPTION 'item % is marked %, below its collection''s marking', NEW.id, NEW.marking
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER items_marking_floor BEFORE INSERT OR UPDATE ON catalog.items
    FOR EACH ROW EXECUTE FUNCTION catalog.item_marking_floor();

-- An upload ends cataloged, quarantined or rejected once the pipeline has its bytes.
UPDATE ingest.uploads SET status = 'cataloged' WHERE status = 'committed';
ALTER TABLE ingest.uploads DROP CONSTRAINT uploads_status_check;
ALTER TABLE ingest.uploads ADD CONSTRAINT uploads_status_check
    CHECK (status IN ('pending', 'cataloged', 'quarantined', 'rejected', 'abandoned'));

-- One row per blob version the queue announced: the pipeline's step machine (ING-1, ING-2).
-- `received` is the only in-flight stage; the others are outcomes.
CREATE TABLE ingest.jobs (
    blob_name  text NOT NULL,
    etag       text NOT NULL,
    upload_id  uuid REFERENCES ingest.uploads (id),
    stage      text NOT NULL DEFAULT 'received'
               CHECK (stage IN ('received', 'cataloged', 'quarantined', 'rejected')),
    reason     text,
    attempts   integer NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (blob_name, etag),
    CHECK ((stage IN ('quarantined', 'rejected')) = (reason IS NOT NULL))
);

CREATE INDEX jobs_upload ON ingest.jobs (upload_id);

-- An outcome, once reached, never changes (ING-2).
CREATE FUNCTION ingest.job_outcome_is_final() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.stage <> 'received' AND NEW.stage IS DISTINCT FROM OLD.stage THEN
        RAISE EXCEPTION 'job %@% is already %', OLD.blob_name, OLD.etag, OLD.stage
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER jobs_outcome_is_final BEFORE UPDATE ON ingest.jobs
    FOR EACH ROW EXECUTE FUNCTION ingest.job_outcome_is_final();
