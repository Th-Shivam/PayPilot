-- 0001_extensions.sql
-- Enables the Postgres extensions PayPilot depends on.
--
-- Supabase hosts extensions in a dedicated "extensions" schema which is already
-- on the default search_path, so `vector` type references need no qualification.
-- Both statements are safe to re-run.

create extension if not exists vector;

-- gen_random_uuid() lives in pgcrypto on older Postgres. On PG13+ it is built
-- in, but requesting pgcrypto explicitly keeps the migration portable to a
-- plain Postgres container for local verification.
create extension if not exists pgcrypto;
