-- M1 hardening: one item per content per collection (ING-9).

-- Items cataloged before dedup may already share a checksum within a collection. They stay (an
-- item and its raw blob are never removed by the pipeline, ING-3), flagged so the rule below
-- covers every item from now on and none of those. The earliest of each set is the original.
ALTER TABLE catalog.items ADD COLUMN predates_dedup boolean NOT NULL DEFAULT false;
UPDATE catalog.items i SET predates_dedup = true
WHERE EXISTS (
    SELECT 1 FROM catalog.items o
    WHERE o.collection_id = i.collection_id AND o.sha256 = i.sha256
      AND (o.created_at, o.id) < (i.created_at, i.id)
);
-- New items can't claim the exemption.
CREATE FUNCTION catalog.dedup_exemption_is_closed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.predates_dedup AND (TG_OP = 'INSERT' OR NOT OLD.predates_dedup) THEN
        RAISE EXCEPTION 'item % cannot be exempted from dedup', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER items_dedup_exemption_is_closed BEFORE INSERT OR UPDATE ON catalog.items
    FOR EACH ROW EXECUTE FUNCTION catalog.dedup_exemption_is_closed();

CREATE UNIQUE INDEX items_one_per_content ON catalog.items (collection_id, sha256)
    WHERE NOT predates_dedup;
