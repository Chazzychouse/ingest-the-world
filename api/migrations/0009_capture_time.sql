-- Capture time (M2 slice 1): when the data was captured, as its header says, and the time a
-- `datetime` search matches.

-- Inclusive at both ends; an instant is a range whose ends are equal. Null when the header
-- doesn't say.
ALTER TABLE catalog.items
    ADD COLUMN captured tstzrange
    CHECK (captured IS NULL OR NOT (isempty(captured) OR lower_inf(captured) OR upper_inf(captured)));

-- What an item's STAC `datetime` shows, and so what a `datetime` filter matches (SRCH-1): when it
-- was captured, or, when its header doesn't say, the instant it was cataloged.
ALTER TABLE catalog.items
    ADD COLUMN datetime_range tstzrange GENERATED ALWAYS AS (
        coalesce(captured, tstzrange(created_at, created_at, '[]'))
    ) STORED;

CREATE INDEX items_datetime_range ON catalog.items USING gist (datetime_range);
