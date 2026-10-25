-- TenderScout AI - 003: sector and department facets.
--
-- The browse filters gained a sector (IT & Telecom, Construction & Works,
-- ...) and a department (Information Technology, Health, ...) facet. Both
-- are derived by the collector from text the source already states (see
-- collector/taxonomy.py) and are optional: rows collected before this
-- migration simply have NULL until the next collection run or
-- `python collector/main.py --backfill-taxonomy`.
--
-- Idempotent: safe to re-run on an existing database.

alter table opportunities
    add column if not exists sector text,
    add column if not exists department text;

create index if not exists idx_opportunities_sector_department
    on opportunities (sector, department);
