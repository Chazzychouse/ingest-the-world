-- M2 slice 6: footprint views. Opening one is audited (ACC-5); its tiles then name it instead of
-- repeating the filters, and are served only to its opener until it expires.
CREATE TABLE catalog.footprint_views (
    id         uuid PRIMARY KEY,
    subject    text NOT NULL,
    filters    jsonb NOT NULL CHECK (jsonb_typeof(filters) = 'object'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);

CREATE INDEX footprint_views_expiry ON catalog.footprint_views (expires_at);
