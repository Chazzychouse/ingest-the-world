-- M2 slice 5: assets as STAC shows them, and the name the original is served under.

-- What each derivative is for, recorded from its recipe: a title, and STAC asset roles
-- (`thumbnail`; `data` and `cloud-optimized` for a COG). Derivatives made before this only knew
-- their role, so that is what they show.
ALTER TABLE catalog.assets
    ADD COLUMN title text,
    ADD COLUMN stac_roles text[];
UPDATE catalog.assets SET title = role, stac_roles = ARRAY[role];
ALTER TABLE catalog.assets
    ALTER COLUMN title SET NOT NULL,
    ALTER COLUMN stac_roles SET NOT NULL,
    ADD CONSTRAINT assets_title_check CHECK (length(title) BETWEEN 1 AND 100),
    ADD CONSTRAINT assets_stac_roles_check CHECK (cardinality(stac_roles) BETWEEN 1 AND 8),
    -- `/items/{id}/assets/original` is the original itself; no derivative may take its name.
    ADD CONSTRAINT assets_role_not_original CHECK (role <> 'original');
