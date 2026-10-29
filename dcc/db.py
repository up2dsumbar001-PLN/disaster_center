"""Thin PostgreSQL access layer (psycopg 3) with a small thread-safe pool.

All SQL uses %s placeholders. JSON values are passed as text with an
explicit ::jsonb cast (use db.J(value)), so the same SQL works with any
DB-API driver that follows psycopg's paramstyle.
"""
from __future__ import annotations

import json
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def J(value: Any) -> str:
    """Serialise a value for a %s::jsonb parameter."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _default_connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


class Database:
    def __init__(self, dsn: str, connect: Callable[[str], Any] | None = None, size: int = 8):
        self.dsn = dsn
        self._connect = connect or _default_connect
        self._pool: "queue.LifoQueue" = queue.LifoQueue()
        self._size = size
        self._created = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ pool
    def _acquire(self):
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self._size:
                self._created += 1
                try:
                    return self._connect(self.dsn)
                except Exception:
                    self._created -= 1
                    raise
        return self._pool.get(timeout=30)

    def _release(self, conn, broken: bool = False):
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1
            return
        self._pool.put(conn)

    @contextmanager
    def tx(self):
        """Transaction scope yielding a cursor-like helper."""
        conn = self._acquire()
        broken = False
        try:
            yield Tx(conn)
            conn.commit()
        except BaseException as exc:
            try:
                conn.rollback()
            except Exception:
                broken = True
            if _is_connection_error(exc):
                broken = True
            raise
        finally:
            self._release(conn, broken)

    # ------------------------------------------------------ shortcuts
    def all(self, sql: str, params: Iterable | None = None) -> list[dict]:
        with self.tx() as t:
            return t.all(sql, params)

    def one(self, sql: str, params: Iterable | None = None) -> dict | None:
        with self.tx() as t:
            return t.one(sql, params)

    def run(self, sql: str, params: Iterable | None = None) -> int:
        with self.tx() as t:
            return t.run(sql, params)

    def migrate(self) -> None:
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.tx() as t:
            t.run("SELECT pg_advisory_xact_lock(814503)")
            for stmt in _split_sql(sql):
                t.run(stmt)

    def close(self):
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
            except Exception:
                pass
        self._created = 0


class Tx:
    def __init__(self, conn):
        self.conn = conn

    def _exec(self, sql, params):
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params) if params is not None else None)
        return cur

    def all(self, sql, params=None) -> list[dict]:
        cur = self._exec(sql, params)
        return list(cur.fetchall()) if cur.description else []

    def one(self, sql, params=None) -> dict | None:
        cur = self._exec(sql, params)
        return cur.fetchone() if cur.description else None

    def run(self, sql, params=None) -> int:
        cur = self._exec(sql, params)
        return cur.rowcount

    def scalar(self, sql, params=None):
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))


def _is_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name in {"OperationalError", "InterfaceError", "ConnectionError", "BrokenPipeError"}


def _split_sql(sql: str) -> list[str]:
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf).rstrip().rstrip(";"))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out
