"""Guardrails for the text-to-SQL escape hatch (PLAN.md §4 "Local query
interface"). Pure functions over a string, no I/O or DuckDB connection here —
the defense-in-depth also includes the read-only connection and
`enable_external_access = false` in `analytics/db.py`, verified empirically
to block `read_csv` et al. This module is the layer that understands SQL
*structure* rather than trusting a regex, which a regex allowlist can't do:
it catches stacked statements, DDL/DML hidden in the tree, and file-reading
functions however they're spelled.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class SqlGuardError(Exception):
    """A query failed validation before ever reaching DuckDB."""


# Exact function names known to read files, install/load extensions, or
# otherwise reach outside the database — belt-and-suspenders alongside
# enable_external_access=false, which already blocks these at execution time.
_DENIED_FUNCS_EXACT = {
    "READ_CSV",
    "READ_JSON",
    "READ_JSON_OBJECTS",
    "READ_PARQUET",
    "READ_NDJSON",
    "READ_NDJSON_OBJECTS",
    "READ_TEXT",
    "READ_BLOB",
    "GLOB",
    "INSTALL",
    "LOAD",
}

# Statement/clause types with no business appearing inside a read-only query.
_DENIED_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Command,
    exp.Pragma,
    exp.Set,
    exp.Install,
)


def _func_name(func: exp.Func) -> str:
    if isinstance(func, exp.Anonymous):
        return str(func.this).upper()
    name = func.sql_name()
    return (name or func.__class__.__name__).upper()


def _is_denied_func(name: str) -> bool:
    return name in _DENIED_FUNCS_EXACT or name.endswith("_AUTO")


def validate(sql: str) -> exp.Query:
    """Parse and validate `sql`. Returns the parsed AST on success — callers
    should render *that* back out (`.sql(dialect="duckdb")`) rather than
    execute the caller's original string, so what runs is provably what was
    validated rather than something a parser/execution mismatch could smuggle
    past. Raises SqlGuardError with a specific reason on any violation.
    """
    sql = sql.strip()
    if not sql:
        raise SqlGuardError("Empty query.")

    try:
        statements = [s for s in sqlglot.parse(sql, dialect="duckdb") if s is not None]
    except sqlglot.errors.SqlglotError as exc:
        raise SqlGuardError(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise SqlGuardError(
            f"Exactly one SQL statement is allowed; found {len(statements)}. Stacked "
            "statements (e.g. separated by ';') are not permitted."
        )
    stmt = statements[0]

    if not isinstance(stmt, exp.Query):
        raise SqlGuardError(
            f"Only read-only SELECT/WITH queries are allowed; got {type(stmt).__name__}."
        )

    for node in stmt.walk():
        if isinstance(node, _DENIED_NODE_TYPES):
            raise SqlGuardError(f"{type(node).__name__} is not allowed inside a query.")

    for func in stmt.find_all(exp.Func):
        name = _func_name(func)
        if _is_denied_func(name):
            raise SqlGuardError(
                f"Function {name}() is not allowed — it can read files or load "
                "extensions, which this read-only query interface never permits."
            )

    return stmt
