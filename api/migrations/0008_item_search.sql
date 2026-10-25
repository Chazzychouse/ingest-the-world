-- Item reads and search (M1 slice 5): the item access rule, search text, paging order.

-- An item is readable when its collection is and the reader's clearance covers the item's own
-- marking, which may be above its collection's (ACC-1, ACC-3). Returned as (collection,
-- marking) pairs, few enough to hash once per query: filter with
-- `(i.collection_id, i.marking) IN (SELECT collection_id, marking FROM authz.readable_scopes(...))`.
CREATE FUNCTION authz.readable_scopes(
    subject text, roles text[], groups text[], clearances text[]
) RETURNS TABLE (collection_id text, marking text)
LANGUAGE sql STABLE AS $$
    SELECT r.id, m.code
    FROM authz.readable_collections(subject, roles, groups, clearances) AS r (id)
    CROSS JOIN authz.markings m
    WHERE authz.cleared_for(m.code, clearances)
$$;

-- What `q` terms are matched against: filename and tags, lowercased, one per line. Terms never
-- hold whitespace, so none can match across a line break. Immutable for the generated column:
-- `array_to_string` is only stable in general, but is immutable over text.
CREATE FUNCTION catalog.search_text(filename text, tags text[]) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT lower(filename || E'\n' || array_to_string(tags, E'\n'))
$$;

ALTER TABLE catalog.items
    ADD COLUMN search_text text GENERATED ALWAYS AS (catalog.search_text(filename, tags)) STORED;

CREATE INDEX items_search_text ON catalog.items USING gin (search_text gin_trgm_ops);
-- Search order: newest first, ties broken by id, so a cursor is a position in a total order.
CREATE INDEX items_newest ON catalog.items (created_at DESC, id DESC);
