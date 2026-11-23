-- Region master for walk area selection (Issue #43).
--
-- Additive only: one new table, no change to existing tables or rows. The table
-- starts empty; rows come from scripts/sync_regions.py, which converts the official
-- 법정동코드 file of 행정표준코드관리시스템 (see docs/issues/43/HANDOFF.md).
--
-- Codes (one system, 법정동코드; no 행정동 or geocoder codes):
--   SIDO                2 digits, the first two digits of the 10-digit 법정동코드 ("11")
--   SIGUNGU             5 digits, the first five digits ("11110"); the parent is its SIDO
--   SPECIAL_SELECTABLE  application code "36_SPECIAL_ALL" (세종특별자치시 전체), parent "36"
-- region_level is VARCHAR(20), not the VARCHAR(16) of the first Issue draft:
-- 'SPECIAL_SELECTABLE' has 18 characters.
-- Abolished regions are kept with is_active = false, never deleted, so stored
-- references stay valid.
--
-- Number: 022 was unused in every branch and open PR on 2026-11-23. It does not
-- depend on 017 to 021; it only needs PostgreSQL 11+.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/022_regions.sql
-- Re-running after a successful apply is a no-op.
-- Rollback (only while nothing references regions, after a backup):
--   DROP TABLE IF EXISTS regions;
BEGIN;

CREATE TABLE IF NOT EXISTS regions (
    region_code VARCHAR(20) PRIMARY KEY,
    region_name VARCHAR(100) NOT NULL,
    region_level VARCHAR(20) NOT NULL,
    parent_region_code VARCHAR(20) CONSTRAINT fk_regions_parent_region_code REFERENCES regions(region_code),
    is_active BOOLEAN NOT NULL,
    CONSTRAINT ck_regions_level CHECK (region_level IN ('SIDO', 'SIGUNGU', 'SPECIAL_SELECTABLE')),
    -- Only SIDO codes have two characters, so a parent that passes the FK is a SIDO.
    CONSTRAINT ck_regions_hierarchy CHECK (
        (region_level = 'SIDO' AND parent_region_code IS NULL AND length(region_code) = 2)
        OR (region_level = 'SIGUNGU' AND length(region_code) = 5
            AND parent_region_code = substr(region_code, 1, 2))
        OR (region_level = 'SPECIAL_SELECTABLE' AND region_code = '36_SPECIAL_ALL'
            AND parent_region_code = '36')),
    CONSTRAINT ck_regions_code_digits CHECK (region_level = 'SPECIAL_SELECTABLE' OR region_code ~ '^[0-9]+$'),
    CONSTRAINT ck_regions_name_not_blank CHECK (trim(region_name) <> '')
);

-- GET /regions/sigungu and the selection check read children of one SIDO.
CREATE INDEX IF NOT EXISTS ix_regions_parent_active ON regions (parent_region_code, is_active);

COMMIT;
