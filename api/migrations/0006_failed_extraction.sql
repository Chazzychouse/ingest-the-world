-- M1 hardening: sandboxed extraction (ING-8).

-- A reader that crashed, hung or ran out of memory leaves its item `failed`.
ALTER TABLE catalog.items DROP CONSTRAINT items_extraction_check;
ALTER TABLE catalog.items ADD CONSTRAINT items_extraction_check
    CHECK (extraction IN ('extracted', 'unsupported', 'unreadable', 'over-budget', 'failed'));
