-- Foundation: extensions, markings, collection governance, grants, audit log.
-- Collections here hold platform governance (marking, retention). The matching
-- STAC collection lives in pgSTAC from M1 onward, under the same id.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA authz;
CREATE SCHEMA catalog;
CREATE SCHEMA audit;

-- Markings are data, not code: adding one is an INSERT.
CREATE TABLE authz.markings (
    code         text PRIMARY KEY,
    rank         integer NOT NULL UNIQUE,
    label        text NOT NULL,
    banner_color text NOT NULL
);

INSERT INTO authz.markings (code, rank, label, banner_color) VALUES
    ('UNCLASSIFIED', 0,  'UNCLASSIFIED', '#007a33'),
    ('CUI',          10, 'CUI',          '#502b85');

CREATE TABLE catalog.collections (
    id             text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    title          text NOT NULL,
    description    text NOT NULL DEFAULT '',
    marking        text NOT NULL REFERENCES authz.markings (code),
    cui_categories text[] NOT NULL DEFAULT '{}',
    dissemination  text[] NOT NULL DEFAULT '{}',
    -- {"retain_days": int|null, "tiering": [{"after_days": int, "tier": "cool"|...}], "legal_hold": bool}
    retention      jsonb NOT NULL DEFAULT '{}',
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT cui_details_only_on_cui CHECK (
        marking = 'CUI' OR (cui_categories = '{}' AND dissemination = '{}')
    )
);

CREATE TABLE authz.collection_grants (
    collection_id  text NOT NULL REFERENCES catalog.collections (id) ON DELETE CASCADE,
    principal_type text NOT NULL CHECK (principal_type IN ('role', 'group', 'user')),
    principal      text NOT NULL,
    access         text NOT NULL CHECK (access IN ('read', 'write', 'curate')),
    PRIMARY KEY (collection_id, principal_type, principal)
);

-- Highest marking rank among the clearances a principal holds; unknown codes are ignored.
CREATE FUNCTION authz.clearance_rank(clearances text[]) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(rank), 0) FROM authz.markings WHERE code = ANY (clearances)
$$;

-- The single read-authorization predicate: clearance must cover the marking, and the
-- principal needs a grant unless they are an Admin. Admins never bypass clearance.
-- Search, the STAC API and downloads all filter through this function.
CREATE FUNCTION authz.readable_collections(
    subject text, roles text[], groups text[], clearances text[]
) RETURNS SETOF text
LANGUAGE sql STABLE AS $$
    SELECT c.id
    FROM catalog.collections c
    JOIN authz.markings m ON m.code = c.marking
    WHERE m.rank <= authz.clearance_rank(clearances)
      AND (
          'Admin' = ANY (roles)
          OR EXISTS (
              SELECT 1 FROM authz.collection_grants g
              WHERE g.collection_id = c.id
                AND (   (g.principal_type = 'role'  AND g.principal = ANY (roles))
                     OR (g.principal_type = 'group' AND g.principal = ANY (groups))
                     OR (g.principal_type = 'user'  AND g.principal = subject))
          )
      )
$$;

CREATE TABLE audit.events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor_sub   text,
    actor_name  text,
    action      text NOT NULL,
    target_type text,
    target_id   text,
    marking     text,
    outcome     text NOT NULL CHECK (outcome IN ('allowed', 'denied', 'error')),
    ip          inet,
    request_id  text,
    detail      jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX audit_events_at ON audit.events (at DESC);
CREATE INDEX audit_events_actor ON audit.events (actor_sub, at DESC);
CREATE INDEX audit_events_target ON audit.events (target_type, target_id, at DESC);

-- Append-only, even for the table owner.
CREATE FUNCTION audit.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.events is append-only';
END
$$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit.events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
