"""Minimal PostgreSQL wire-protocol client used ONLY by the test-suite.

Production uses psycopg 3 (see requirements.txt). This shim implements the
small subset of the psycopg API that dcc.db relies on (connect, cursor,
execute with %s placeholders, fetchone/fetchall as dict rows, commit,
rollback, close) so the SQL can be exercised against a real PostgreSQL
server in environments where psycopg cannot be installed.
Supports trust authentication only.
"""
from __future__ import annotations

import datetime as _dt
import json
import socket
import struct


class Error(Exception):
    pass


class IntegrityError(Error):
    pass


def _quote(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return "'" + v.isoformat() + "'"
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    s = str(v)
    if "\x00" in s:
        raise Error("NUL in string")
    return "'" + s.replace("'", "''") + "'"


def _interpolate(sql: str, params) -> str:
    if params is None:
        return sql
    out, i, n, it = [], 0, len(sql), iter(params)
    while i < n:
        c = sql[i]
        if c == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == "s":
                out.append(_quote(next(it)))
                i += 2
                continue
            if nxt == "%":
                out.append("%")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _parse_ts(s: str):
    s = s.replace(" ", "T", 1)
    if len(s) >= 3 and (s[-3] in "+-") and s[-3:].lstrip("+-").isdigit():
        s += ":00"
    return _dt.datetime.fromisoformat(s)


_CONV = {
    16: lambda s: s == "t",
    20: int, 21: int, 23: int, 26: int,
    700: float, 701: float, 1700: float,
    114: json.loads, 3802: json.loads,
    1184: _parse_ts,
    1114: lambda s: _dt.datetime.fromisoformat(s.replace(" ", "T", 1)),
    1082: _dt.date.fromisoformat,
}


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows: list = []
        self.description = None
        self.rowcount = -1

    def execute(self, sql, params=None):
        q = _interpolate(sql, params)
        self.rows, self.description, self.rowcount = self.conn._query(q)
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        r, self.rows = self.rows, []
        return r

    def __iter__(self):
        while self.rows:
            yield self.rows.pop(0)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Connection:
    def __init__(self, host="127.0.0.1", port=5432, user="postgres", dbname="postgres", **_):
        self.sock = socket.create_connection((host, int(port)))
        body = b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in
                        {"user": user, "database": dbname, "client_encoding": "UTF8", "TimeZone": "UTC"}.items()) + b"\0"
        self.sock.sendall(struct.pack("!ii", len(body) + 8, 196608) + body)
        self.in_tx = False
        self.closed = False
        while True:
            t, payload = self._read()
            if t == b"R":
                if struct.unpack("!i", payload[:4])[0] != 0:
                    raise Error("shim supports trust auth only")
            elif t == b"E":
                raise Error(self._err(payload))
            elif t == b"Z":
                break

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise Error("connection closed")
            buf += chunk
        return buf

    def _read(self):
        t = self._recv(1)
        ln = struct.unpack("!i", self._recv(4))[0]
        return t, self._recv(ln - 4)

    @staticmethod
    def _err(payload):
        fields = {}
        for part in payload.split(b"\0"):
            if part:
                fields[chr(part[0])] = part[1:].decode("utf-8", "replace")
        return fields.get("C", ""), fields.get("M", "")

    def _send_query(self, q):
        data = q.encode("utf-8") + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!i", len(data) + 4) + data)

    def _query(self, q):
        if not self.in_tx:
            self._simple("BEGIN")
            self.in_tx = True
        return self._simple(q)

    def _simple(self, q):
        self._send_query(q)
        rows, desc, err, rowcount = [], None, None, -1
        cols = []
        while True:
            t, p = self._read()
            if t == b"T":
                (nf,) = struct.unpack("!h", p[:2])
                off = 2
                cols = []
                for _ in range(nf):
                    end = p.index(b"\0", off)
                    name = p[off:end].decode()
                    off = end + 1
                    _tbl, _col, oid, _sz, _mod, _fmt = struct.unpack("!ihihih", p[off:off + 18])
                    off += 18
                    cols.append((name, oid))
                desc = [(c[0],) for c in cols]
            elif t == b"D":
                (nf,) = struct.unpack("!h", p[:2])
                off = 2
                row = {}
                for i in range(nf):
                    (ln,) = struct.unpack("!i", p[off:off + 4])
                    off += 4
                    if ln == -1:
                        val = None
                    else:
                        raw = p[off:off + ln].decode("utf-8")
                        off += ln
                        conv = _CONV.get(cols[i][1])
                        val = conv(raw) if conv else raw
                    row[cols[i][0]] = val
                rows.append(row)
            elif t == b"C":
                tag = p.rstrip(b"\0").decode()
                last = tag.split(" ")[-1]
                rowcount = int(last) if last.isdigit() else -1
            elif t == b"E":
                err = self._err(p)
            elif t == b"Z":
                break
        if err:
            code, msg = err
            if code.startswith("23"):
                raise IntegrityError(msg)
            raise Error(f"{code}: {msg}")
        return rows, desc, rowcount

    def cursor(self, *a, **k):
        return Cursor(self)

    def commit(self):
        if self.in_tx:
            self._simple("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self._simple("ROLLBACK")
            finally:
                self.in_tx = False

    def close(self):
        if not self.closed:
            try:
                self.sock.sendall(b"X" + struct.pack("!i", 4))
            finally:
                self.sock.close()
                self.closed = True


def connect(conninfo: str = "", **kw):
    params = {}
    for part in conninfo.split():
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v
    params.update(kw)
    return Connection(host=params.get("host", "127.0.0.1"), port=params.get("port", 5432),
                      user=params.get("user", "postgres"), dbname=params.get("dbname", "postgres"))
