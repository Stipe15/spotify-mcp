"""DuckDB connection helpers, shared by ingest (read-write) and the analytics
tools (read-only)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from spotify_mcp.analytics.sqlguard import validate

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


class QueryTimeoutError(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    sql_executed: str


def connect_rw(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Read-write connection, schema ensured. Used only by ingest."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(_SCHEMA_SQL)
    return con


def connect_ro(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Read-only connection for analytics tools/queries. Locked down against
    the filesystem/network-reaching functions guarded queries must never
    reach — see PLAN.md §4 "Local query interface"."""
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("SET enable_external_access = false")
    con.execute("SET allow_unsigned_extensions = false")
    con.execute("SET threads = 2")
    con.execute("SET memory_limit = '1GB'")
    return con


def run_guarded(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    max_rows: int,
    timeout_s: float,
) -> QueryResult:
    """Validate `sql` (sqlguard), execute it on a bounded connection, cap the
    row count, and enforce a wall-clock timeout via `con.interrupt()` from a
    watchdog thread — verified empirically to actually cancel a running
    DuckDB query (see PLAN.md §4). Raises `sqlguard.SqlGuardError` for a
    rejected query and `QueryTimeoutError` if it ran too long.

    The validated AST is re-rendered rather than the caller's original
    string executed, so what runs is provably what passed validation.
    """
    parsed = validate(sql)
    rendered = parsed.sql(dialect="duckdb")
    wrapped = f"SELECT * FROM ({rendered}) AS _spotify_mcp_q LIMIT {max_rows + 1}"

    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        relation = con.execute(wrapped)
        columns = [d[0] for d in relation.description]
        rows = relation.fetchall()
    except duckdb.InterruptException as exc:
        raise QueryTimeoutError(f"Query exceeded {timeout_s:.0f}s and was cancelled.") from exc
    finally:
        timer.cancel()

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    return QueryResult(
        columns=columns,
        rows=[dict(zip(columns, row, strict=True)) for row in rows],
        row_count=len(rows),
        truncated=truncated,
        sql_executed=wrapped,
    )
