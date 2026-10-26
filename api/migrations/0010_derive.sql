-- Derivatives (M2 slice 3): the outbox that hands cataloged items to the derive stage, the derive
-- jobs that stage runs, and the assets they leave in `derived/` (ING-7, ING-11, ING-12).

-- Messages to publish, written in the same transaction as the change they announce, so a
-- committed change is always announced and an uncommitted one never is. A relay sends unpublished
-- rows to their queue at least once and then stamps them.
CREATE TABLE ingest.outbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic        text NOT NULL CHECK (topic IN ('derive')),
    body         jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    created_at   timestamptz NOT NULL,
    published_at timestamptz
);

CREATE INDEX outbox_unpublished ON ingest.outbox (id) WHERE published_at IS NULL;

-- One row per (item, role, recipe version) the derive stage has taken on. `received` is the only
-- in-flight stage; `derived` and `failed` are final, and a failure never touches the item (ING-11).
-- Only cataloged files are items, so nothing quarantined or rejected can have one.
CREATE TABLE ingest.derive_jobs (
    item_id        uuid NOT NULL REFERENCES catalog.items (id),
    role           text NOT NULL,
    recipe_version integer NOT NULL CHECK (recipe_version > 0),
    stage          text NOT NULL DEFAULT 'received'
                   CHECK (stage IN ('received', 'derived', 'failed')),
    reason         text,
    attempts       integer NOT NULL DEFAULT 1 CHECK (attempts > 0),
    last_error     text,
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    PRIMARY KEY (item_id, role, recipe_version),
    CHECK ((stage = 'failed') = (reason IS NOT NULL))
);

CREATE FUNCTION ingest.derive_outcome_is_final() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.stage <> 'received' AND NEW.stage IS DISTINCT FROM OLD.stage THEN
        RAISE EXCEPTION 'derive job %/%/v% is already %',
            OLD.item_id, OLD.role, OLD.recipe_version, OLD.stage
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER derive_jobs_outcome_is_final BEFORE UPDATE ON ingest.derive_jobs
    FOR EACH ROW EXECUTE FUNCTION ingest.derive_outcome_is_final();

-- A derivative of an item: at most one per role and recipe version (ING-12), each in its own
-- blob in `derived/`, never overwritten. `lineage` holds STAC `processing:` fields.
CREATE TABLE catalog.assets (
    item_id        uuid NOT NULL REFERENCES catalog.items (id),
    role           text NOT NULL CHECK (role ~ '^[a-z][a-z0-9-]{0,31}$'),
    recipe_version integer NOT NULL CHECK (recipe_version > 0),
    media_type     text NOT NULL,
    blob_name      text NOT NULL UNIQUE,
    size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
    sha256         text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    lineage        jsonb NOT NULL CHECK (jsonb_typeof(lineage) = 'object'),
    created_at     timestamptz NOT NULL,
    PRIMARY KEY (item_id, role, recipe_version)
);
