"""Command line entry point.

  python -m dcc serve                 jalankan server (host/port dari .env, default 0.0.0.0:18085)
  python -m dcc check                 uji koneksi database dan migrasi skema
  python -m dcc import-kml <file>     impor aset KML/KMZ langsung dari baris perintah
  python -m dcc reset-password <user> setel ulang kata sandi akun (interaktif)
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m dcc", description="Disaster Information Center DCC")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    sub.add_parser("check")
    imp = sub.add_parser("import-kml")
    imp.add_argument("file")
    rp = sub.add_parser("reset-password")
    rp.add_argument("username")
    ini = sub.add_parser("init-db", help="buat role & database aplikasi (butuh superuser PostgreSQL)")
    ini.add_argument("--host", default="127.0.0.1")
    ini.add_argument("--port", type=int, default=5432)
    ini.add_argument("--superuser", default="postgres")
    ini.add_argument("--dbname", default="disaster_center")
    ini.add_argument("--user", default="dcc_app")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "init-db":
        return init_db(args)

    from .config import load_settings
    from .db import Database

    settings = load_settings()
    cmd = args.cmd or "serve"
    if cmd == "serve":
        from logging.handlers import RotatingFileHandler

        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "dcc.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger("uvicorn").addHandler(fh)
        import uvicorn

        from .app import create_app

        app = create_app(settings)
        # Satu worker: collector BMKG & antrean WA berjalan di proses ini.
        uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=settings.trust_proxy_headers, use_colors=False,
                    log_level="info", access_log=False)
        return 0
    db = Database(settings.database_url)
    if cmd == "check":
        db.migrate()
        row = db.one("SELECT version() AS v, current_database() AS d, current_user AS u")
        print(f"OK  database={row['d']} user={row['u']}\n    {row['v']}")
        n = db.one("SELECT count(*) AS n FROM assets WHERE active")["n"]
        print(f"    aset aktif: {n}")
        return 0
    if cmd == "import-kml":
        from . import assets, kml_import

        db.migrate()
        path = Path(args.file)
        records, problems = kml_import.parse_assets(kml_import.open_kml_stream(path.name, path.read_bytes()))
        res = assets.publish_snapshot(db, records, "kml", path.name, "cli", problems)
        print(f"Snapshot #{res['snapshot_id']}: {res['total']} aset (baru {res['inserted']}, diperbarui {res['updated']}, "
              f"nonaktif {res['deactivated']}), catatan {len(problems)}")
        for k, v in sorted(res["by_kind"].items()):
            print(f"  {k:10s} {v}")
        return 0
    if cmd == "reset-password":
        from . import security

        pw = getpass.getpass("Kata sandi baru: ")
        n = db.run("UPDATE users SET password_hash=%s, must_change_password=true, active=true WHERE lower(username)=%s",
                   (security.hash_password(pw), args.username.lower()))
        print("Berhasil." if n else "Akun tidak ditemukan.")
        return 0 if n else 1
    p.print_help()
    return 1


def init_db(args) -> int:
    """Create (or update) the application role and database without needing psql.exe.

    App password is read from env DCC_INIT_APP_PASSWORD (set by setup.ps1);
    the superuser password is asked interactively (or env DCC_INIT_SUPER_PASSWORD).
    """
    import os
    import re

    import psycopg
    from psycopg import sql

    for v in (args.dbname, args.user):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v):
            print(f"Nama tidak valid: {v}")
            return 2
    app_pw = os.environ.get("DCC_INIT_APP_PASSWORD", "")
    if len(app_pw) < 12:
        print("DCC_INIT_APP_PASSWORD belum diisi (minimal 12 karakter).")
        return 2
    su_pw = os.environ.get("DCC_INIT_SUPER_PASSWORD") or getpass.getpass(
        f"Kata sandi superuser PostgreSQL '{args.superuser}' di {args.host}:{args.port}: ")
    common = dict(host=args.host, port=args.port, user=args.superuser, password=su_pw, connect_timeout=10)
    try:
        conn = psycopg.connect(dbname="postgres", autocommit=True, **common)
    except psycopg.OperationalError as exc:
        print(f"GAGAL terhubung sebagai {args.superuser}: {str(exc).strip()}")
        print("Periksa kata sandi, port (lihat postgresql.conf), atau nama superuser (parameter -PgSuperUser).")
        return 1
    with conn:
        print(conn.execute("SELECT version()").fetchone()[0])
        role = sql.Identifier(args.user)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (args.user,)).fetchone():
            conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, sql.Literal(app_pw)))
            print(f"Role {args.user} sudah ada; kata sandinya diperbarui.")
        else:
            conn.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE")
                         .format(role, sql.Literal(app_pw)))
            print(f"Role {args.user} dibuat.")
        db = sql.Identifier(args.dbname)
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.dbname,)).fetchone():
            conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(db, role))
            print(f"Database {args.dbname} sudah ada (data lama dipertahankan).")
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(db, role))
            print(f"Database {args.dbname} dibuat.")
        conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(db))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
        enc = conn.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s",
                           (args.dbname,)).fetchone()[0]
        if enc != "UTF8":
            print(f"PERINGATAN: encoding database {args.dbname} = {enc}, disarankan UTF8.")
    with psycopg.connect(dbname=args.dbname, autocommit=True, **common) as c2:
        c2.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(args.user)))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
