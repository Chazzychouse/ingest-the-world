-- One definition of "cleared": the principal's highest clearance covers the marking's rank.
-- Returns NULL for an unknown marking, so callers can tell "unknown" from "not cleared".
-- Collection creation and the read predicate both use it, instead of each comparing ranks.

CREATE FUNCTION authz.cleared_for(marking text, clearances text[]) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT m.rank <= authz.clearance_rank(clearances) FROM authz.markings m WHERE m.code = marking
$$;

CREATE OR REPLACE FUNCTION authz.readable_collections(
    subject text, roles text[], groups text[], clearances text[]
) RETURNS SETOF text
LANGUAGE sql STABLE AS $$
    SELECT c.id
    FROM catalog.collections c
    WHERE authz.cleared_for(c.marking, clearances)
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
