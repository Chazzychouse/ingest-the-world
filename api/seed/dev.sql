-- Development and demo seed. Idempotent; never run against production.
-- Users (Keycloak realm `ingest`):
--   alice  Admin        + Clearance.CUI
--   carol  Contributor  + Clearance.CUI
--   bob    Reader       (UNCLASSIFIED only)

INSERT INTO catalog.collections (id, title, description, marking, cui_categories, dissemination, retention, created_by)
VALUES
    ('site-survey-alpha', 'Site Survey Alpha',
     'Drone imagery, LiDAR and photogrammetry meshes for the Alpha facility survey.',
     'UNCLASSIFIED', '{}', '{}',
     '{"retain_days": null, "tiering": [{"after_days": 30, "tier": "cool"}, {"after_days": 180, "tier": "cold"}], "legal_hold": false}',
     'seed'),
    ('bio-lab-readings', 'Bio Lab Readings',
     'Environmental sensor and assay readings from field kits, batched daily.',
     'UNCLASSIFIED', '{}', '{}',
     '{"retain_days": 1825, "tiering": [{"after_days": 90, "tier": "cool"}], "legal_hold": false}',
     'seed'),
    ('range-ops-cui', 'Range Operations',
     'Range infrastructure models and terrain. Controlled.',
     'CUI', '{CRIT}', '{FEDCON}',
     '{"retain_days": null, "tiering": [{"after_days": 30, "tier": "cool"}], "legal_hold": true}',
     'seed')
ON CONFLICT (id) DO NOTHING;

INSERT INTO authz.collection_grants (collection_id, principal_type, principal, access) VALUES
    ('site-survey-alpha', 'role', 'Reader',      'read'),
    ('site-survey-alpha', 'role', 'Contributor', 'write'),
    ('bio-lab-readings',  'role', 'Reader',      'read'),
    ('bio-lab-readings',  'role', 'Contributor', 'write'),
    -- Every Reader is granted this collection, but only CUI-cleared users can read it:
    -- the grant and the clearance are checked independently.
    ('range-ops-cui',     'role', 'Reader',      'read'),
    ('range-ops-cui',     'role', 'Contributor', 'write')
ON CONFLICT DO NOTHING;
