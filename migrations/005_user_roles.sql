-- Run through scripts.migrate_backend so structure and backup are checked first.
-- Enum names match SQLAlchemy's existing UserRole mapping (uppercase in PostgreSQL).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'userrole' AND n.nspname = current_schema()
    ) THEN
        CREATE TYPE userrole AS ENUM ('USER', 'SHELTER', 'ADMIN');
    END IF;
END;
$$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS role userrole NOT NULL DEFAULT 'USER';
