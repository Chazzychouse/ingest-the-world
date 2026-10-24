-- Upload sessions and write access (ACC-7).
--
-- Grant matching moves into one function, `authz.granted_collections`, so the read and write
-- rules can't disagree about which grants a principal holds or how Admins are treated.

-- Access levels, lowest first; a grant of one level includes those below it.
CREATE FUNCTION authz.access_rank(access text) RETURNS integer
LANGUAGE sql IMMUTABLE AS $$
    SELECT array_position(ARRAY['read', 'write', 'curate'], access)
$$;

-- Collections a principal holds at least `minimum` access on, through a grant to one of their
-- roles (already expanded by the role hierarchy), groups or their own subject. Admins hold every
-- access on every collection. Clearance is not considered here: callers combine the two.
CREATE FUNCTION authz.granted_collections(
    subject text, roles text[], groups text[], minimum text
) RETURNS SETOF text
LANGUAGE sql STABLE AS $$
    SELECT c.id FROM catalog.collections c WHERE 'Admin' = ANY (roles)
    UNION
    SELECT g.collection_id
    FROM authz.collection_grants g
    WHERE authz.access_rank(g.access) >= authz.access_rank(minimum)
      AND (   (g.principal_type = 'role'  AND g.principal = ANY (roles))
           OR (g.principal_type = 'group' AND g.principal = ANY (groups))
           OR (g.principal_type = 'user'  AND g.principal = subject))
$$;

CREATE OR REPLACE FUNCTION authz.readable_collections(
    subject text, roles text[], groups text[], clearances text[]
) RETURNS SETOF text
LANGUAGE sql STABLE AS $$
    SELECT c.id
    FROM catalog.collections c
    WHERE authz.cleared_for(c.marking, clearances)
      AND c.id IN (SELECT authz.granted_collections(subject, roles, groups, 'read'))
$$;

-- Upload access: cleared for the marking, a Contributor or higher (roles arrive expanded by the
-- hierarchy, so an Admin carries Contributor), and a write grant, which Admins hold implicitly.
CREATE FUNCTION authz.writable_collections(
    subject text, roles text[], groups text[], clearances text[]
) RETURNS SETOF text
LANGUAGE sql STABLE AS $$
    SELECT c.id
    FROM catalog.collections c
    WHERE authz.cleared_for(c.marking, clearances)
      AND 'Contributor' = ANY (roles)
      AND c.id IN (SELECT authz.granted_collections(subject, roles, groups, 'write'))
$$;

CREATE SCHEMA ingest;

-- One row per upload session. The bytes go straight from the browser to `landing/blob_name`
-- with a signed URL; the API never touches them.
CREATE TABLE ingest.uploads (
    id             uuid PRIMARY KEY,
    collection_id  text NOT NULL REFERENCES catalog.collections (id),
    filename       text NOT NULL,
    size_bytes     bigint NOT NULL CHECK (size_bytes > 0),
    content_type   text,
    sha256         text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    tags           text[] NOT NULL DEFAULT '{}',
    blob_name      text NOT NULL UNIQUE,
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'committed', 'abandoned')),
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL,
    url_expires_at timestamptz NOT NULL CHECK (url_expires_at > created_at)
);

CREATE INDEX uploads_collection ON ingest.uploads (collection_id, created_at DESC);
CREATE INDEX uploads_creator ON ingest.uploads (created_by, created_at DESC);
