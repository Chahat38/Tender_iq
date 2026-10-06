-- Minimal stand-in for Supabase's `auth` schema, used ONLY when the
-- migration is applied to a plain Postgres (local dev / CI container).
-- A real Supabase project already has auth.users, auth.uid() and the
-- anon/authenticated/service_role roles, so this file is a no-op there.
--
-- Applied automatically by scripts/migrate_sqlite_to_postgres.py when
-- `select to_regclass('auth.users')` returns NULL.

create schema if not exists auth;

create table if not exists auth.users (
    id uuid primary key,
    email text,
    created_at timestamptz not null default now()
);

-- Mirrors Supabase's auth.uid(): the subject claim of the request JWT,
-- set for a session with
--   select set_config('request.jwt.claim.sub', '<uuid>', true);
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

-- Supabase's three API roles (RLS policies target `authenticated`).
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin;
    end if;
end $$;

grant usage on schema public to anon, authenticated, service_role;
