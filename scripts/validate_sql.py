#!/usr/bin/env python3
"""Parse the Supabase migration with the real Postgres grammar.

Uses ``pglast`` (libpg_query - the parser PostgreSQL itself ships) so a
syntax error in ``supabase/migrations/*.sql`` fails in CI instead of on
the first `supabase db push`. Statements built dynamically inside DO
blocks (the RLS policy loops) are rendered per table and parsed too.

Run from the repository root:

    python scripts/validate_sql.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
EXTRA_SQL = [REPO_ROOT / "scripts" / "pg_auth_shim.sql"]


def render_format(template: str, table: str) -> str:
    """Render one PG ``format('...', t[, t])`` template for ``table``.

    Mirrors PostgreSQL's ``format()`` for the two specifiers the
    migration uses: ``%s`` (plain substitution) and ``%I`` (identifier
    quoting).
    """
    template = template.replace("''", "'")
    # PG's %I (quoted identifier) parses the same as a bare name here.
    template = template.replace("%I", "%s")
    if "%" not in template:
        return template
    args = (table, table, table)
    last_error: Exception | None = None
    for size in range(1, len(args) + 1):
        try:
            return template % args[:size]
        except (TypeError, ValueError) as exc:  # wrong tuple size
            last_error = exc
    raise ValueError(
        f"cannot render format template {template!r} for {table!r}: {last_error}"
    )


def statements_to_check(sql: str) -> list[tuple[str, str]]:
    """Return ``(label, statement)`` pairs covering every generated DDL.

    Static statements are parsed as-is; the DO blocks are unpacked so
    their ``execute format(...)`` templates are rendered for each table
    in the loop's array and parsed individually.
    """
    out: list[tuple[str, str]] = []

    # Statements outside DO blocks.
    without_dos = re.sub(r"do \$\$.*?\$\$;", "", sql, flags=re.DOTALL)
    for statement in _split_statements(without_dos):
        out.append(("static", statement))

    # DO blocks: loop arrays + execute format() templates inside.
    for match in re.finditer(
        r"do \$\$(.*?)\$\$;", sql, flags=re.DOTALL
    ):
        body = match.group(1)
        arrays = re.findall(
            r"foreach t in array array\[(.*?)\]\s*loop(.*?)end loop;",
            body,
            flags=re.DOTALL,
        )
        if not arrays:
            out.append(("do-block", "do $$ " + body + " $$;"))
            continue
        for tables_blob, loop_body in arrays:
            tables = re.findall(r"'([a-z_]+)'", tables_blob)
            templates = re.findall(r"execute\s+format\(\s*'((?:[^']|'')*)'", loop_body)
            if not templates:
                out.append(("do-loop(no-op)", "do $$ " + loop_body + " $$;"))
            for table in tables:
                for template in templates:
                    out.append(
                        (f"policy[{table}]", render_format(template, table))
                    )
    return out


def _split_statements(sql: str) -> list[str]:
    """Split a script into statements, aware of quotes and comments."""
    statements: list[str] = []
    buffer: list[str] = []
    in_dollar_quote = False
    in_string = False
    in_line_comment = False
    i = 0
    while i < len(sql):
        if in_line_comment:
            buffer.append(sql[i])
            if sql[i] == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_dollar_quote:
            if sql.startswith("$$", i):
                in_dollar_quote = False
                buffer.append("$$")
                i += 2
                continue
            buffer.append(sql[i])
            i += 1
            continue
        if in_string:
            char = sql[i]
            buffer.append(char)
            if char == "'":
                if sql.startswith("''", i):  # escaped quote
                    buffer.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if sql.startswith("--", i):
            in_line_comment = True
            buffer.append("--")
            i += 2
            continue
        if sql.startswith("$$", i):
            in_dollar_quote = True
            buffer.append("$$")
            i += 2
            continue
        char = sql[i]
        if char == "'":
            in_string = True
            buffer.append(char)
            i += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            i += 1
            continue
        buffer.append(char)
        i += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def validate_file(path: Path) -> list[str]:
    """Parse one SQL file; return a list of human-readable errors."""
    import pglast

    errors: list[str] = []
    sql = path.read_text(encoding="utf-8")
    try:
        pglast.parse_sql(sql)
    except pglast.parser.ParseError as exc:
        # Whole-file parse fails when a DO block has a syntax error or a
        # template contains ``;``; continue to statement-level checks.
        errors.append(f"{path.name}: {exc}")

    for label, statement in statements_to_check(sql):
        try:
            pglast.parse_sql(statement)
        except pglast.parser.ParseError as exc:
            errors.append(f"{path.name} [{label}]: {exc}\n  {statement[:200]}")
    return errors


def main() -> int:
    try:
        import pglast  # noqa: F401
    except ImportError:
        print("error: pglast is required (pip install pglast)", file=sys.stderr)
        return 2

    files = sorted(MIGRATIONS_DIR.glob("*.sql")) + [
        p for p in EXTRA_SQL if p.exists()
    ]
    if not files:
        print(f"error: no SQL files under {MIGRATIONS_DIR}", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    for path in files:
        errors = validate_file(path)
        if errors:
            all_errors.extend(errors)
        else:
            print(f"ok: {path.relative_to(REPO_ROOT)}")
    for error in all_errors:
        print(f"FAIL: {error}", file=sys.stderr)
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
