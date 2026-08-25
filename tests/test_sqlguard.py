"""Attack corpus for the text-to-SQL guard — the primary safety target for
Phase 4 (PLAN.md). Every one of these must be rejected before ever reaching
DuckDB; the connection-level enable_external_access=false is defense in
depth, not the only line.
"""

from __future__ import annotations

import pytest

from spotify_mcp.analytics.sqlguard import SqlGuardError, validate

ATTACK_QUERIES = [
    ("stacked statement", "SELECT * FROM plays; DROP TABLE plays;"),
    ("stacked statement, no trailing semicolon", "SELECT 1; DROP TABLE plays"),
    ("bare DDL", "DROP TABLE plays"),
    ("bare DML insert", "INSERT INTO plays VALUES (1)"),
    ("bare DML delete", "DELETE FROM plays"),
    ("bare DML update", "UPDATE plays SET ms_played = 0"),
    ("create table", "CREATE TABLE evil (x INT)"),
    ("alter table", "ALTER TABLE plays ADD COLUMN x INT"),
    ("attach another db", "ATTACH 'other.db' AS other"),
    ("detach", "DETACH other"),
    ("pragma", "PRAGMA database_list"),
    ("set", "SET memory_limit='10GB'"),
    ("copy to file", "COPY plays TO 'out.csv'"),
    ("copy from file", "COPY plays FROM 'in.csv'"),
    ("install extension", "INSTALL httpfs"),
    ("load extension", "LOAD httpfs"),
    ("read_csv", "SELECT * FROM read_csv('x.csv')"),
    ("read_csv_auto", "SELECT * FROM read_csv_auto('x.csv')"),
    ("read_json", "SELECT * FROM read_json('x.json')"),
    ("read_json_auto", "SELECT * FROM read_json_auto('x.json')"),
    ("read_parquet", "SELECT * FROM read_parquet('x.parquet')"),
    ("glob", "SELECT glob('*.csv')"),
    ("arbitrary _auto function", "SELECT some_weird_auto(1)"),
    ("install hidden in CTE", "WITH x AS (SELECT 1) INSTALL httpfs"),
    ("ddl hidden behind comment on first line", "-- looks safe\nDROP TABLE plays"),
    ("empty query", ""),
    ("whitespace only", "   \n\t  "),
    ("not sql at all", "this is not sql"),
]


@pytest.mark.parametrize("label,sql", ATTACK_QUERIES, ids=[a[0] for a in ATTACK_QUERIES])
def test_attack_corpus_is_rejected(label: str, sql: str):
    with pytest.raises(SqlGuardError):
        validate(sql)


ALLOWED_QUERIES = [
    "SELECT * FROM plays",
    "SELECT * FROM plays LIMIT 10",
    "SELECT artist_name, COUNT(*) FROM plays GROUP BY artist_name",
    "WITH recent AS (SELECT * FROM plays WHERE played_date > '2026-01-01') SELECT * FROM recent",
    "SELECT * FROM plays UNION SELECT * FROM plays",
    "SELECT * FROM plays -- a trailing comment that mentions DROP TABLE plays",
    "SELECT * FROM dim_artist ORDER BY play_count DESC",
]


@pytest.mark.parametrize("sql", ALLOWED_QUERIES)
def test_legitimate_queries_are_allowed(sql: str):
    validate(sql)  # must not raise


def test_comment_smuggled_second_statement_does_not_bypass_the_single_statement_rule():
    """The classic regex-allowlist bypass: a semicolon-separated second
    statement hidden inside a SQL comment. A real parser sees one statement;
    the guard must agree — and must NOT be fooled into seeing an injected
    DROP as live SQL either way."""
    sql = "SELECT * FROM plays -- ; DROP TABLE plays; --"
    validate(sql)  # the DROP is commented out, so this is legitimately fine


def test_validate_returns_the_parsed_ast_not_the_raw_string():
    result = validate("select   *   from   plays")
    rendered = result.sql(dialect="duckdb")
    assert "SELECT" in rendered.upper()


def test_error_messages_name_the_specific_violation():
    with pytest.raises(SqlGuardError, match="Drop"):
        validate("DROP TABLE plays")
    with pytest.raises(SqlGuardError, match="READ_CSV"):
        validate("SELECT * FROM read_csv('x.csv')")
    with pytest.raises(SqlGuardError, match="Exactly one"):
        validate("SELECT 1; SELECT 2;")
