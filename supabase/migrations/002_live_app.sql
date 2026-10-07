-- TenderScout AI - 002: live web app compatibility.
--
-- Applied on top of 001_init.sql for the public web app
-- (github.com/Chahat38/tender) which reads direct from Supabase:
--
--   1. `opportunities.estimated_budget` / `bid_security` - the app's
--      Opportunity type carries them (optional; the collector does not
--      populate them yet, they stay NULL).
--   2. Public (anon) SELECT for the tender-facing tables so unauthenticated
--      visitors can browse tenders, sources, documents, requirements,
--      updates and AI cache - the collector (service role / connection
--      string) keeps writing them; anon never writes.
--   3. Explicit grants so the read path does not depend on Supabase
--      default privileges having been configured for the schema.

alter table opportunities
    add column if not exists estimated_budget text,
    add column if not exists bid_security text;

-- Public read: anon SELECT on tender-facing tables (RLS still prevents
-- any anon write; the collector bypasses RLS via its authenticated
-- service connection). Postgres has no `create policy if not exists`,
-- so re-runs first drop the policy.
do $$
declare t text;
begin
    foreach t in array array[
        'procurement_sources', 'opportunities', 'opportunity_documents',
        'opportunity_requirements', 'opportunity_updates', 'opportunity_ai'
    ] loop
        execute format('drop policy if exists %I on %I', 'public_read', t);
    end loop;
end $$;

create policy "public_read" on procurement_sources for select to anon using (true);
create policy "public_read" on opportunities for select to anon using (true);
create policy "public_read" on opportunity_documents for select to anon using (true);
create policy "public_read" on opportunity_requirements for select to anon using (true);
create policy "public_read" on opportunity_updates for select to anon using (true);
create policy "public_read" on opportunity_ai for select to anon using (true);

-- Explicit grants (defensive; matches Supabase's default privileges).
grant select on procurement_sources to anon, authenticated;
grant select on opportunities to anon, authenticated;
grant select on opportunity_documents to anon, authenticated;
grant select on opportunity_requirements to anon, authenticated;
grant select on opportunity_updates to anon, authenticated;
grant select on opportunity_ai to anon, authenticated;

-- The app reads/writes its user tables (profiles, saved_opportunities,
-- rfp_sections, ...) as an anonymous user only through the demo fallback;
-- real per-user data arrives through Supabase Auth + RLS. No extra grants
-- needed: Supabase default privileges already allow authenticated users
-- access to the tables behind the owner-scoped policies from 001_init.sql.

-- Keep 002 idempotent for re-runs (collector/re-run safety).