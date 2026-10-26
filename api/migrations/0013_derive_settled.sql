-- M2 slice 7: when an item's derivatives have settled, so its upload can report `ready` (ING-13).

-- One row per item whose every applicable derivative has an outcome, written by the derive stage
-- when it finishes an item and removed when it starts on one again (a redelivered request, a new
-- recipe version), so a row never stands beside an in-flight derive job.
CREATE TABLE ingest.derive_settled (
    item_id    uuid PRIMARY KEY REFERENCES catalog.items (id),
    settled_at timestamptz NOT NULL
);

-- Items derived before this table existed are settled unless a job of theirs is still running.
INSERT INTO ingest.derive_settled (item_id, settled_at)
SELECT i.id, now() FROM catalog.items i
WHERE NOT EXISTS (
    SELECT 1 FROM ingest.derive_jobs j WHERE j.item_id = i.id AND j.stage = 'received'
);
