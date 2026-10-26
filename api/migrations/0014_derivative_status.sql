-- M2 slice 8: an item's derivative status, shown with the item and by its upload's stage.

-- Whether an item's derivatives have settled (ING-13): `ready` once every one that applies has an
-- outcome, `deriving` until then. The one place the rule lives; items and uploads both ask it.
CREATE FUNCTION ingest.derivatives(item_id uuid) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN EXISTS (SELECT 1 FROM ingest.derive_settled s WHERE s.item_id = $1)
                THEN 'ready' ELSE 'deriving' END
$$;

-- The derivatives an item lacks because they failed, as {role: reason}: each role whose newest
-- recipe version failed and that has no asset from an earlier one. A role still in flight, or one
-- an older recipe made, is not missing.
CREATE FUNCTION ingest.derivative_failures(item_id uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_object_agg(newest.role, newest.reason), '{}'::jsonb)
    FROM (
        SELECT DISTINCT ON (j.role) j.role, j.stage, j.reason
        FROM ingest.derive_jobs j
        WHERE j.item_id = $1
        ORDER BY j.role, j.recipe_version DESC
    ) newest
    WHERE newest.stage = 'failed'
      AND NOT EXISTS (
          SELECT 1 FROM catalog.assets a WHERE a.item_id = $1 AND a.role = newest.role
      )
$$;
