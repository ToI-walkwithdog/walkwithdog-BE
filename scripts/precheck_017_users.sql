-- Read-only pre-check before migration 017 (Issues #30, #32). It changes nothing.
--
--   psql -X -v ON_ERROR_STOP=1 -d <DB_NAME> -f scripts/precheck_017_users.sql
--
-- Runs inside a READ ONLY transaction that is rolled back at the end. Prints counts,
-- column, index and object names only: never emails, passwords or login IDs, so the
-- output can be pasted into a PR. Works whether or not 016 and 017 were applied.
BEGIN TRANSACTION READ ONLY;

\echo '== 1. Accounts by role'
SELECT role::text AS role, count(*) AS accounts FROM users GROUP BY role ORDER BY 1;

\echo '== 2. users columns'
SELECT column_name, data_type, character_maximum_length AS max_length, is_nullable
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = 'users'
ORDER BY ordinal_position;

\echo '== 3. users constraints'
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint WHERE conrelid = 'users'::regclass ORDER BY conname;

\echo '== 4. users indexes'
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = current_schema() AND tablename = 'users' ORDER BY indexname;

\echo '== 5. Migration markers (no history table: true means the objects a file creates exist)'
WITH col(t, c) AS (SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = current_schema()),
     idx(n) AS (SELECT indexname FROM pg_indexes WHERE schemaname = current_schema())
SELECT m.migration, m.applied FROM (VALUES
    ('001 matching_results.survey_id', EXISTS (SELECT 1 FROM col WHERE t = 'matching_results' AND c = 'survey_id')),
    ('002 auth_sessions', to_regclass('auth_sessions') IS NOT NULL),
    ('003 shelters.external_code', EXISTS (SELECT 1 FROM col WHERE t = 'shelters' AND c = 'external_code')),
    ('004 chat_sessions', to_regclass('chat_sessions') IS NOT NULL),
    ('005 users.role', EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'role')),
    ('006 users.shelter_id', EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'shelter_id')),
    ('007 walk_requests', to_regclass('walk_requests') IS NOT NULL),
    ('008 walks.moving_duration_seconds', EXISTS (SELECT 1 FROM col WHERE t = 'walks' AND c = 'moving_duration_seconds')),
    ('009 dog_walk_date_slots', to_regclass('dog_walk_date_slots') IS NOT NULL),
    ('011 walk_requests.rejection_reason_code', EXISTS (SELECT 1 FROM col WHERE t = 'walk_requests' AND c = 'rejection_reason_code')),
    ('012 walks.last_latitude', EXISTS (SELECT 1 FROM col WHERE t = 'walks' AND c = 'last_latitude')),
    ('013 walk_incidents.client_report_id', EXISTS (SELECT 1 FROM col WHERE t = 'walk_incidents' AND c = 'client_report_id')),
    ('014 users.phone', EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'phone')),
    ('015 walk_reviews.rating', EXISTS (SELECT 1 FROM col WHERE t = 'walk_reviews' AND c = 'rating')),
    ('016 users.login_id', EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'login_id')),
    ('016 uq_users_login_id_lower', EXISTS (SELECT 1 FROM idx WHERE n = 'uq_users_login_id_lower')),
    ('016 uq_users_nickname_lower', EXISTS (SELECT 1 FROM idx WHERE n = 'uq_users_nickname_lower')),
    ('016 uq_users_email_lower', EXISTS (SELECT 1 FROM idx WHERE n = 'uq_users_email_lower')),
    ('017 users.email removed', NOT EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'email'))
) AS m(migration, applied);

\echo '== 6. Columns named like email in any table'
SELECT table_schema, table_name, column_name FROM information_schema.columns
WHERE column_name ILIKE '%email%' AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2, 3;

\echo '== 7. Views and functions that mention email'
SELECT 'view' AS kind, schemaname || '.' || viewname AS name FROM pg_views
WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND definition ILIKE '%email%'
UNION ALL
SELECT 'function', n.nspname || '.' || p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND p.prosrc ILIKE '%email%'
ORDER BY 1, 2;

\echo '== 8. Objects that depend on users.email (these would block 017)'
SELECT d.classid::regclass AS catalog, d.objid, d.deptype
FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.refobjid = 'users'::regclass AND a.attname = 'email' AND d.deptype = 'n';

\echo '== 9. Password state by role (no values)'
SELECT role::text AS role,
       count(*) FILTER (WHERE password IS NULL) AS null_password,
       count(*) FILTER (WHERE password IS NOT NULL AND password NOT LIKE 'scrypt$%') AS not_scrypt,
       count(*) FILTER (WHERE password LIKE 'scrypt$%') AS scrypt
FROM users GROUP BY role ORDER BY 1;

\echo '== 10. login_id state by role'
SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
               AND table_name = 'users' AND column_name = 'login_id') AS has_login_id \gset
\if :has_login_id
SELECT role::text AS role,
       count(*) FILTER (WHERE login_id IS NULL) AS null_id,
       count(*) FILTER (WHERE login_id IS NOT NULL AND btrim(login_id) = '') AS blank_id,
       count(*) FILTER (WHERE login_id IS NOT NULL AND btrim(login_id) <> ''
                        AND login_id !~ '^[A-Za-z0-9]{4,20}$') AS breaks_new_rule,
       count(*) FILTER (WHERE login_id ~ '_') AS has_underscore,
       count(*) FILTER (WHERE login_id <> lower(login_id)) AS not_lowercase
FROM users GROUP BY role ORDER BY 1;
\echo '-- login IDs shared by more than one account, ignoring case (count of groups only)'
SELECT count(*) AS duplicate_groups FROM (
    SELECT lower(login_id) FROM users WHERE login_id IS NOT NULL GROUP BY lower(login_id) HAVING count(*) > 1
) AS groups;
\else
\echo 'users.login_id does not exist: 016 is not applied. Every account needs an approved login_id mapping.'
\endif

\echo '== 11. Nickname state against the 016 rules (VARCHAR(20), case-insensitive unique)'
SELECT count(*) FILTER (WHERE char_length(nickname) > 20) AS over_20_chars,
       (SELECT count(*) FROM (SELECT lower(nickname) FROM users GROUP BY lower(nickname)
                              HAVING count(*) > 1) AS groups) AS duplicate_groups
FROM users;

ROLLBACK;
