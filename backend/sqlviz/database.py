from __future__ import annotations

import csv
import io
import math
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterator

import pymysql
from sqlglot import exp

from . import settings
from .models import Cell, Snapshot

_limits = settings.limits()
MAX_ROWS = _limits["max_rows"]
MAX_STEPS = _limits["max_steps"]
MAX_TABLES = _limits["max_tables"]
MAX_BYTES = _limits["max_upload_bytes"]
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class InputError(Exception):
    pass


def quote(name: str) -> str:
    if not name or len(name) > 64 or "\x00" in name:
        raise InputError("Identifiers must contain 1-64 non-NUL characters.")
    return "`" + name.replace("`", "``") + "`"


def schema(workspace: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", workspace):
        raise InputError("Invalid workspace ID.")
    return "viz_" + workspace


def connect(database: str | None = None, **overrides) -> pymysql.Connection:
    config = settings.mysql()
    options = dict(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        ssl={"ca": config.ssl_ca} if config.ssl_ca else None,
        database=database,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=8,
        write_timeout=8,
        autocommit=False,
        local_infile=False,
    )
    options.update(overrides)
    connection = pymysql.connect(**options)
    with connection.cursor() as cursor:
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={config.query_timeout_ms:d}")
        cursor.execute("SET SESSION innodb_lock_wait_timeout=3")
        cursor.execute("SET SESSION cte_max_recursion_depth=100")
    return connection


@contextmanager
def workspace_connection(workspace: str) -> Iterator[pymysql.Connection]:
    name = schema(workspace)
    with _locks_guard:
        lock = _locks.setdefault(workspace, threading.RLock())
    with lock:
        connection = connect(name)
        try:
            yield connection
        finally:
            connection.rollback()
            connection.close()


def create_workspace() -> str:
    workspace = uuid.uuid4().hex
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {quote(schema(workspace))}")
    finally:
        connection.close()
    return workspace


def drop_workspace(workspace: str) -> None:
    with workspace_connection(workspace) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {quote(schema(workspace))}")
    with _locks_guard:
        _locks.pop(workspace, None)


def cell(value: object) -> Cell:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputError("Non-finite floating point values cannot be visualized.")
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        microseconds = value // timedelta(microseconds=1)
        seconds, fraction = divmod(abs(microseconds), 1_000_000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{'-' if microseconds < 0 else ''}{hours:02}:{minutes:02}:{seconds:02}" + (f".{fraction:06}" if fraction else "")
    if isinstance(value, bytes):
        return "0x" + value.hex()
    raise InputError(f"Unsupported result value type: {type(value).__name__}.")


def snapshot(connection: pymysql.Connection, query: exp.Query) -> Snapshot:
    bounded = query.copy()
    limit = bounded.args.get("limit")
    if (
        limit is None
        or not isinstance(limit.expression, exp.Literal)
        or int(limit.expression.this) > MAX_ROWS + 1
    ):
        bounded = bounded.limit(MAX_ROWS + 1)
    with connection.cursor() as cursor:
        cursor.execute(bounded.sql(dialect="mysql"))
        columns = [column[0] for column in cursor.description or ()]
        rows = cursor.fetchall()
    return Snapshot(
        columns=columns,
        rows=[[cell(value) for value in row] for row in rows[:MAX_ROWS]],
        truncated=len(rows) > MAX_ROWS,
    )


def table_names(connection: pymysql.Connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SHOW TABLES")
        return [row[0] for row in cursor.fetchall()]


def check_dataset_size(connection: pymysql.Connection) -> None:
    names = table_names(connection)
    if len(names) > MAX_TABLES:
        raise InputError(f"A workspace supports at most {MAX_TABLES} tables.")
    with connection.cursor() as cursor:
        for name in names:
            cursor.execute(f"SELECT COUNT(*) FROM {quote(name)}")
            if cursor.fetchone()[0] > MAX_ROWS:
                raise InputError(f"Table {name!r} exceeds the {MAX_ROWS}-row teaching-dataset limit.")


def describe_tables(connection: pymysql.Connection) -> list[dict]:
    tables = []
    with connection.cursor() as cursor:
        for name in table_names(connection):
            cursor.execute(f"SHOW COLUMNS FROM {quote(name)}")
            columns = [{"name": row[0], "type": row[1], "key": row[3]} for row in cursor.fetchall()]
            cursor.execute(f"SELECT COUNT(*) FROM {quote(name)}")
            count = cursor.fetchone()[0]
            data = snapshot(connection, exp.select("*").from_(exp.to_table(name, quoted=True)))
            tables.append(dict(name=name, columns=columns, row_count=count, data=data.model_dump()))
    return tables


def load_rows(
    connection: pymysql.Connection,
    table: str,
    columns: list[str],
    rows: list[list[object]],
) -> None:
    quote(table)
    if not columns or len({name.casefold() for name in columns}) != len(columns):
        raise InputError("Column headers must be nonempty and unique (case-insensitive).")
    for column in columns:
        quote(column)
    if len(rows) > MAX_ROWS or any(len(row) != len(columns) for row in rows):
        raise InputError(f"Use at most {MAX_ROWS} rows, each with the same width as the header.")
    if len(table_names(connection)) >= MAX_TABLES:
        raise InputError(f"A workspace supports at most {MAX_TABLES} tables.")

    def sql_type(index: int) -> str:
        values = [row[index] for row in rows if row[index] is not None]
        if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            if all(-(2**63) <= value < 2**63 for value in values):
                return "BIGINT"
        if values and all(isinstance(value, (int, float, Decimal)) for value in values):
            return "DOUBLE" if any(isinstance(value, float) for value in values) else "DECIMAL(65,20)"
        if values and all(isinstance(value, datetime) for value in values):
            return "DATETIME(6)"
        if values and all(isinstance(value, date) and not isinstance(value, datetime) for value in values):
            return "DATE"
        if values and all(isinstance(value, timedelta) for value in values):
            return "TIME(6)"
        if values and all(isinstance(value, bytes) for value in values):
            return "LONGBLOB"
        return "LONGTEXT"

    definitions = ", ".join(f"{quote(name)} {sql_type(index)}" for index, name in enumerate(columns))
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {quote(table)} ({definitions}) ENGINE=InnoDB")
        try:
            if rows:
                placeholders = ", ".join(["%s"] * len(columns))
                cursor.executemany(f"INSERT INTO {quote(table)} VALUES ({placeholders})", rows)
            connection.commit()
        except (pymysql.MySQLError, InputError):
            connection.rollback()
            cursor.execute(f"DROP TABLE {quote(table)}")
            raise


def read_csv(content: bytes) -> tuple[list[str], list[list[object]]]:
    if len(content) > MAX_BYTES:
        raise InputError("CSV uploads must be smaller than 2 MB.")
    try:
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        header = next(reader, None)
        if not header:
            raise InputError("The CSV must have a header row.")
        rows: list[list[object]] = []
        for row in reader:
            values: list[object] = []
            for value in row:
                if value == "":
                    values.append(None)
                elif re.fullmatch(r"-?(0|[1-9][0-9]*)", value) and len(value) < 19:
                    values.append(int(value))
                elif re.fullmatch(r"-?[0-9]{1,30}\.[0-9]{1,20}", value):
                    values.append(Decimal(value))
                else:
                    values.append(value)
            rows.append(values)
            if len(rows) > MAX_ROWS:
                raise InputError(f"CSV files may contain at most {MAX_ROWS} data rows.")
    except (UnicodeDecodeError, csv.Error) as error:
        raise InputError(f"Invalid UTF-8 CSV: {error}") from error
    return header, rows
