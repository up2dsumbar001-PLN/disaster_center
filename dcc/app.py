"""Starlette application: HTTP API + static frontend."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import io
import json
import logging
import re
import threading
import time
import traceback
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import analysis, appsettings, assets, cap, collector, customers, geo, kml_import, photos, security, tomorrow
from .config import BASE_DIR, Settings, load_settings
from .db import Database, J

log = logging.getLogger("dcc")
STATIC_DIR = BASE_DIR / "static"
WIB = dt.timezone(dt.timedelta(hours=7))
SEVERITIES = ("rendah", "sedang", "tinggi", "kritis")
STATUSES = ("dilaporkan", "diverifikasi", "penanganan", "selesai")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


class Ctx:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.index = assets.AssetIndex()
        self.collector = collector.Collector(self)
        self.import_job: dict = {"state": "idle"}
        self.import_lock = threading.Lock()
        self.started_at = time.time()


# ------------------------------------------------------------ middleware
def client_ip(scope, settings: Settings) -> str | None:
    if settings.trust_proxy_headers:
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-for":
                return v.decode().split(",")[0].strip()
    c = scope.get("client")
    return c[0] if c else None


class NetworkGuard:
    """Reject clients outside DCC_ALLOWED_NETWORKS and add security headers."""

    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        ip = client_ip(scope, self.settings)
        if not self.settings.ip_allowed(ip):
            resp = PlainTextResponse(f"Akses ditolak: alamat {ip} di luar jaringan yang diizinkan.", status_code=403)
            return await resp(scope, receive, send)

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                h += [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"SAMEORIGIN"),
                      (b"referrer-policy", b"strict-origin-when-cross-origin"),
                      (b"content-security-policy",
                       b"default-src 'self'; img-src 'self' data: blob: https:; style-src 'self' 'unsafe-inline'; "
                       b"script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'")]
            await send(message)

        return await self.app(scope, receive, send_wrapped)


# ---------------------------------------------------------------- helpers
class Call:
    def __init__(self, request: Request, user, body, form, ctx: Ctx):
        self.request, self.user, self.body, self.form, self.ctx = request, user, body, form, ctx
        self.params = request.query_params
        self.path = request.path_params
        self.ip = client_ip(request.scope, ctx.settings)

    @property
    def actor(self) -> str:
        return self.user["username"] if self.user else "guest"

    @property
    def role(self) -> str:
        return self.user["role"] if self.user else "guest"

    def audit(self, action, entity_type=None, entity_id=None, detail=None):
        self.ctx.db.run("INSERT INTO audit_events(actor_id, actor_name, action, entity_type, entity_id, detail, ip) "
                        "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                        (self.user["id"] if self.user else None, self.actor, action, entity_type,
                         None if entity_id is None else str(entity_id), J(detail or {}), self.ip))


def api(fn, role: str = "guest", body: str | None = None, max_body: int = 2_000_000):
    """Wrap a sync handler: auth, CSRF header, body parsing, threadpool execution, error mapping."""

    async def endpoint(request: Request):
        ctx: Ctx = request.app.state.ctx
        try:
            if request.method in MUTATING and request.headers.get("x-dcc") != "1":
                raise ApiError(403, "Permintaan ditolak (header X-DCC tidak ada).")
            token = request.cookies.get(security.SESSION_COOKIE)
            user = await asyncio.to_thread(security.session_user, ctx.db, token, ctx.settings.secret_key)
            if not security.has_role(user, role):
                raise ApiError(401 if not user else 403,
                               "Silakan login terlebih dahulu." if not user else "Hak akses tidak mencukupi.")
            if user and user.get("must_change_password") and role != "guest" and fn not in (_change_password, _me):
                raise ApiError(403, "Ganti kata sandi awal terlebih dahulu.", {"must_change_password": True})
            parsed, form = None, None
            if body == "json":
                raw = await request.body()
                if len(raw) > max_body:
                    raise ApiError(413, "Data terlalu besar.")
                try:
                    parsed = json.loads(raw or b"{}")
                except ValueError:
                    raise ApiError(400, "JSON tidak valid.")
                if not isinstance(parsed, dict):
                    raise ApiError(400, "JSON harus berupa objek.")
            elif body == "form":
                cl = int(request.headers.get("content-length") or 0)
                if cl > max_body:
                    raise ApiError(413, f"Unggahan melebihi batas {max_body // (1024 * 1024)} MB.")
                form = await request.form(max_files=5, max_fields=50, max_part_size=max_body)
                files = {}
                for k, v in form.multi_items():
                    if hasattr(v, "read"):
                        data = await v.read()
                        files[k] = (v.filename or "upload", data)
                fields = {k: v for k, v in form.multi_items() if not hasattr(v, "read")}
                await form.close()
                form = {"files": files, "fields": fields}
            call = Call(request, user, parsed, form, ctx)
            result = await asyncio.to_thread(fn, call)
            if isinstance(result, Response):
                return result
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ApiError as e:
            return JSONResponse({"error": e.message, **e.extra}, status_code=e.status)
        except (geo.GeometryError, cap.CapError, kml_import.KmlError, customers.CustomerError, photos.PhotoError) as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        except Exception as e:  # pragma: no cover
            if type(e).__name__ in ("IntegrityError", "UniqueViolation", "ForeignKeyViolation", "CheckViolation"):
                return JSONResponse({"error": "Data bertentangan dengan aturan basis data: " + str(e).splitlines()[0][:200]},
                                    status_code=409)
            log.error("kesalahan server: %s\n%s", e, traceback.format_exc())
            return JSONResponse({"error": "Kesalahan server. Lihat log aplikasi."}, status_code=500)

    endpoint.__name__ = fn.__name__
    return endpoint


def jdefault(o):
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    raise TypeError(type(o))


def jsonable(v):
    return json.loads(json.dumps(v, default=jdefault))


def need(cond, msg, status=422):
    if not cond:
        raise ApiError(status, msg)


def s(v, maxlen, field, required=False, minlen=0) -> str:
    v = "" if v is None else str(v).strip()
    if required and len(v) < max(1, minlen):
        raise ApiError(422, f"{field} wajib diisi" + (f" (minimal {minlen} karakter)." if minlen > 1 else "."))
    if len(v) > maxlen:
        raise ApiError(422, f"{field} maksimal {maxlen} karakter.")
    return v


def parse_dt(v, field) -> dt.datetime:
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ApiError(422, f"{field} tidak valid.")
    if d.tzinfo is None:
        d = d.replace(tzinfo=WIB)
    return d


def int_id(v, field="ID") -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} tidak valid.")
    if n <= 0:
        raise ApiError(400, f"{field} tidak valid.")
    return n


# =================================================================== auth
def _login(c: Call):
    b = c.body
    username = s(b.get("username"), 64, "Nama pengguna", True).lower()
    password = str(b.get("password") or "")
    db = c.ctx.db
    recent = db.one("SELECT count(*) AS n FROM audit_events WHERE action='login_gagal' AND ip=%s AND at > now() - interval '10 minutes'",
                    (c.ip,))
    if recent and recent["n"] >= 10:
        raise ApiError(429, "Terlalu banyak percobaan login. Coba lagi 10 menit lagi.")
    u = db.one("SELECT id, username, role, password_hash, active FROM users WHERE lower(username) = %s", (username,))
    if not u or not u["active"] or not security.verify_password(password, u["password_hash"]):
        c.audit("login_gagal", "user", None, {"username": username})
        raise ApiError(401, "Nama pengguna atau kata sandi salah.")
    token = security.new_session(db, u["id"], c.ctx.settings.secret_key, c.ctx.settings.session_hours, c.ip,
                                 c.request.headers.get("user-agent"))
    c.user = {"id": u["id"], "username": u["username"], "role": u["role"]}
    c.audit("login", "user", u["id"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie(security.SESSION_COOKIE, token, max_age=c.ctx.settings.session_hours * 3600, httponly=True,
                    samesite="lax", secure=c.ctx.settings.cookie_secure, path="/")
    return resp


def _logout(c: Call):
    security.drop_session(c.ctx.db, c.request.cookies.get(security.SESSION_COOKIE), c.ctx.settings.secret_key)
    if c.user:
        c.audit("logout", "user", c.user["id"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


def _me(c: Call):
    return {"user": jsonable(c.user) if c.user else None, "role": c.role}


def _change_password(c: Call):
    need(c.user, "Silakan login.", 401)
    b = c.body
    u = c.ctx.db.one("SELECT password_hash FROM users WHERE id=%s", (c.user["id"],))
    if not security.verify_password(str(b.get("old_password") or ""), u["password_hash"]):
        raise ApiError(422, "Kata sandi lama salah.")
    new = str(b.get("new_password") or "")
    need(new != b.get("old_password"), "Kata sandi baru harus berbeda.")
    try:
        h = security.hash_password(new)
    except ValueError as e:
        raise ApiError(422, str(e))
    c.ctx.db.run("UPDATE users SET password_hash=%s, must_change_password=false WHERE id=%s", (h, c.user["id"]))
    c.audit("ganti_password", "user", c.user["id"])
    return {"ok": True}


# ================================================================== config
def _config(c: Call):
    cfg = appsettings.get(c.ctx.db)
    cats = c.ctx.db.all("SELECT code, label, color FROM incident_categories WHERE active ORDER BY sort, label")
    st = c.ctx.settings
    return {"title": st.site_title, "subtitle": st.site_subtitle, "role": c.role,
            "user": jsonable(c.user) if c.user else None, "settings": appsettings.public_view(cfg),
            "categories": cats, "severities": SEVERITIES, "statuses": STATUSES,
            "kinds": kml_import.KIND_LABEL, "max_photo_mb": st.max_photo_mb, "max_import_mb": st.max_import_mb}


def _health(c: Call):
    rows = c.ctx.db.all("SELECT source, state, checked_at, ok_at, error FROM source_health ORDER BY source")
    return {"status": "ok", "uptime_s": int(time.time() - c.ctx.started_at), "collector_leader": c.ctx.collector.is_leader,
            "assets_loaded": sum(c.ctx.index.counts().values()), "sources": jsonable(rows)}


# ================================================================== assets
def _visible_kinds(c: Call) -> set:
    if c.role != "guest":
        return set(kml_import.KIND_LABEL)
    g = appsettings.get(c.ctx.db)["guest"]
    if not g.get("enabled"):
        raise ApiError(401, "Tampilan tamu dinonaktifkan. Silakan login.")
    return set(g.get("layers") or [])


def _asset_summary(c: Call):
    kinds = _visible_kinds(c)
    db = c.ctx.db
    snap = db.one("SELECT id, source, source_name, started_at, completed_at, counts FROM asset_snapshots "
                  "WHERE state='published' ORDER BY id DESC LIMIT 1")
    by_up3 = {}
    for r in c.ctx.index.by_kind.get("trafo", []):
        k = r["up3"] or "-"
        by_up3[k] = by_up3.get(k, 0) + 1
    out = {"counts": {k: v for k, v in c.ctx.index.counts().items() if k in kinds}, "snapshot": jsonable(snap),
           "trafo_by_up3": by_up3 if "trafo" in kinds else {}}
    if c.role != "guest":
        cs = db.one("SELECT id, filename, uploaded_at, uploaded_by, rows_matched, rows_total, effective_date "
                    "FROM customer_count_sets WHERE active ORDER BY id DESC LIMIT 1")
        out["customer_set"] = jsonable(cs)
        if cs:
            agg = db.one("SELECT count(*) FILTER (WHERE count IS NOT NULL) AS with_data, coalesce(sum(count),0) AS total "
                         "FROM customer_counts WHERE set_id=%s", (cs["id"],))
            out["customer_set"].update({"with_data": agg["with_data"], "total_customers": agg["total"]})
    return out


def _asset_layer(c: Call):
    kind = c.path["kind"]
    need(kind in kml_import.KIND_LABEL, "Jenis lapisan tidak dikenal.", 404)
    if kind not in _visible_kinds(c):
        raise ApiError(403, "Lapisan ini tidak dipublikasikan untuk tamu.")
    data = c.ctx.index.layer(kind, c.params.get("up3") or None)
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return Response(body, media_type="application/json", headers={"Cache-Control": "private, max-age=300",
                                                                  "X-Snapshot": str(c.ctx.index.snapshot_id or "")})


def _asset_get(c: Call):
    aid = int_id(c.path["id"])
    r = c.ctx.index.by_id.get(aid)
    if r is None or r["kind"] not in _visible_kinds(c):
        raise ApiError(404, "Aset tidak ditemukan.")
    d = assets.asset_detail(c.ctx.db, aid, include_customers=c.role != "guest")
    if c.role == "guest":
        d["props"] = {k: v for k, v in (d.get("props") or {}).items() if k in ("folder",)}
    exp = c.ctx.db.all("SELECT e.alert_id, w.title, w.expires FROM alert_exposures e JOIN weather_alerts w ON w.id=e.alert_id "
                       "WHERE e.asset_id=%s AND w.expires > now()", (aid,))
    d["exposures"] = exp
    d["kind_label"] = kml_import.KIND_LABEL.get(d["kind"], d["kind"])
    return jsonable(d)


def _asset_search(c: Call):
    kinds = tuple(k for k in ("trafo", "gi", "gh", "recloser", "lbs", "jtm", "ulp", "pltd", "kantor") if k in _visible_kinds(c))
    return {"results": assets.search(c.ctx.index, c.params.get("q", ""), kinds)}


def _feeders(c: Call):
    feeders: dict = {}
    for r in c.ctx.index.by_kind.get("trafo", []):
        f = feeders.setdefault(r["feeder"] or "(tanpa penyulang)", {"feeder": r["feeder"] or "(tanpa penyulang)", "up3": r["up3"],
                                                                    "trafo": 0, "kva": 0.0})
        f["trafo"] += 1
        f["kva"] += r["capacity_kva"] or 0
    return {"feeders": sorted(feeders.values(), key=lambda x: (x["up3"] or "", x["feeder"]))}


# ---------------------------------------------------------- asset import
def _run_import(ctx: Ctx, filename: str, data: bytes | None, path: Path | None, actor: str):
    job = ctx.import_job
    try:
        job.update(state="running", message="Membaca KML…", filename=filename, started_at=time.time())
        if data is None:
            data = path.read_bytes()
        stream = kml_import.open_kml_stream(filename, data)
        del data
        records, problems = kml_import.parse_assets(stream)
        job["message"] = f"Menyimpan {len(records)} aset ke basis data…"
        res = assets.publish_snapshot(ctx.db, records, "kml", filename, actor, problems)
        job["message"] = "Memuat ulang indeks…"
        ctx.index.load(ctx.db)
        ctx.collector.trigger()
        ctx.db.run("INSERT INTO audit_events(actor_name, action, entity_type, entity_id, detail) VALUES (%s,'impor_aset',"
                   "'asset_snapshot',%s,%s::jsonb)", (actor, str(res["snapshot_id"]), J(res)))
        job.update(state="done", message="Selesai", result=res, problems=problems[:50], finished_at=time.time())
    except Exception as exc:
        log.exception("impor gagal")
        job.update(state="failed", message=str(exc)[:500], finished_at=time.time())


def _start_import(c: Call, filename: str, data: bytes | None, path: Path | None):
    ctx = c.ctx
    with ctx.import_lock:
        if ctx.import_job.get("state") == "running":
            raise ApiError(409, "Impor lain sedang berjalan.")
        ctx.import_job = {"state": "running", "message": "Menyiapkan…", "filename": filename}
    threading.Thread(target=_run_import, args=(ctx, filename, data, path, c.actor), daemon=True).start()
    c.audit("impor_aset_mulai", "asset_snapshot", None, {"filename": filename})
    return {"ok": True, "job": ctx.import_job}


def _import_upload(c: Call):
    f = c.form["files"].get("file")
    need(f, "Pilih berkas KML/KMZ.")
    name, data = f
    need(name.lower().endswith((".kml", ".kmz")), "Gunakan berkas .kml atau .kmz.")
    return _start_import(c, Path(name).name, data, None)


def _server_files(ctx: Ctx) -> list[Path]:
    dirs = [BASE_DIR, ctx.settings.data_dir / "import"]
    out = []
    for d in dirs:
        if d.exists():
            out += [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".kml", ".kmz")]
    return sorted(out)


def _import_server_list(c: Call):
    return {"files": [{"name": p.name, "bytes": p.stat().st_size,
                       "modified": dt.datetime.fromtimestamp(p.stat().st_mtime, WIB).isoformat()} for p in _server_files(c.ctx)]}


def _import_server(c: Call):
    name = s(c.body.get("name"), 300, "Nama berkas", True)
    match = [p for p in _server_files(c.ctx) if p.name == name]
    need(match, "Berkas tidak ditemukan di folder server.", 404)
    return _start_import(c, name, None, match[0])


def _import_status(c: Call):
    return jsonable(c.ctx.import_job)


def _snapshots(c: Call):
    return {"snapshots": jsonable(c.ctx.db.all("SELECT id, source, source_name, started_at, completed_at, state, counts, "
                                               "created_by, left(coalesce(error,''), 2000) AS error FROM asset_snapshots "
                                               "ORDER BY id DESC LIMIT 30"))}


# ------------------------------------------------------------- customers
def _customer_template(c: Call):
    text = customers.template_csv(c.ctx.db)
    return Response("﻿" + text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="template-pelanggan-gardu.csv"'})


def _customer_upload(c: Call):
    f = c.form["files"].get("file")
    need(f, "Pilih berkas Excel/CSV.")
    flds = c.form["fields"]
    res = customers.import_counts(c.ctx.db, Path(f[0]).name, f[1], c.actor, flds.get("key_field") or None,
                                  flds.get("count_field") or None, flds.get("effective_date") or None)
    c.audit("impor_pelanggan", "customer_count_set", res["set_id"], {k: res[k] for k in ("rows_total", "rows_matched")})
    return jsonable(res)


def _customer_sets(c: Call):
    return {"sets": jsonable(c.ctx.db.all("SELECT id, filename, key_field, count_field, uploaded_by, uploaded_at, "
                                          "effective_date, rows_total, rows_matched, rows_unmatched, unmatched_sample, active "
                                          "FROM customer_count_sets ORDER BY id DESC LIMIT 20"))}


# ================================================================= weather
def _alert_dict(w: cap.Warning, exposure: dict, reviews: dict, simulated=False) -> dict:
    return {"id": w.id, "title": w.title, "description": w.description, "instruction": w.instruction,
            "severity": w.severity, "certainty": w.certainty, "urgency": w.urgency, "sent": w.sent,
            "effective": w.effective, "expires": w.expires, "msg_type": w.msg_type, "areas_text": w.areas_text,
            "areas": w.areas, "exposure": exposure, "reviews": reviews.get(w.id, []), "simulated": simulated}


def _exposure_counts(ctx: Ctx, ids: list[int]) -> dict:
    out: dict = {"total": 0}
    for i in ids:
        r = ctx.index.by_id.get(i)
        if r:
            out[r["kind"]] = out.get(r["kind"], 0) + 1
            out["total"] += 1
    return out


def _alerts(c: Call):
    ctx = c.ctx
    cfg = appsettings.get(ctx.db)
    if c.role == "guest" and not cfg["guest"].get("show_alerts"):
        return {"alerts": [], "health": []}
    active = ctx.collector.active_alerts()
    ids = [w.id for w in active]
    exp_rows = ctx.db.all("SELECT alert_id, asset_id FROM alert_exposures WHERE alert_id = ANY(%s::text[])",
                          ("{" + ",".join('"' + i.replace('"', '') + '"' for i in ids) + "}",)) if ids else []
    by_alert: dict = {}
    for r in exp_rows:
        by_alert.setdefault(r["alert_id"], []).append(r["asset_id"])
    reviews: dict = {}
    if ids and c.role != "guest":
        for r in ctx.db.all("SELECT alert_id, user_name, reviewed_at, note FROM alert_reviews WHERE alert_id = ANY(%s::text[]) "
                            "ORDER BY reviewed_at", ("{" + ",".join('"' + i.replace('"', '') + '"' for i in ids) + "}",)):
            reviews.setdefault(r["alert_id"], []).append(jsonable(r))
    alerts = [_alert_dict(w, _exposure_counts(ctx, by_alert.get(w.id, [])), reviews) for w in active]
    if ctx.collector.simulation and c.role != "guest":
        alerts.insert(0, _alert_dict(ctx.collector.simulation, _exposure_counts(ctx, ctx.collector.simulation_exposure), {},
                                     simulated=True))
    health = ctx.db.all("SELECT source, state, checked_at, ok_at, error, detail FROM source_health ORDER BY source")
    for h in health:
        if h["source"] == "bmkg_quake":
            h.pop("detail", None)
    return {"alerts": alerts, "health": jsonable(health), "corridor_m": cfg["corridor_m"],
            "server_time": dt.datetime.now(dt.timezone.utc).isoformat()}


def _alert_assets(c: Call):
    ctx = c.ctx
    aid = c.path["id"]
    if ctx.collector.simulation and aid == ctx.collector.simulation.id:
        ids = ctx.collector.simulation_exposure
    else:
        ids = [r["asset_id"] for r in ctx.db.all("SELECT asset_id FROM alert_exposures WHERE alert_id=%s", (aid,))]
    rows = []
    for i in ids:
        r = ctx.index.by_id.get(i)
        if r:
            rows.append({"id": i, "kind": r["kind"], "name": r["name"], "feeder": r["feeder"], "up3": r["up3"],
                         "ulp": r["ulp"], "lon": r["lon"], "lat": r["lat"], "kva": r["capacity_kva"]})
    rows.sort(key=lambda x: (x["kind"] != "jtm", x["feeder"] or "", x["name"]))
    return {"alert_id": aid, "assets": rows}


def _alert_review(c: Call):
    aid = s(c.path["id"], 300, "ID")
    note = s(c.body.get("note"), 1000, "Catatan")
    c.ctx.db.run("INSERT INTO alert_reviews(alert_id, user_id, user_name, note) VALUES (%s,%s,%s,%s)",
                 (aid, c.user["id"], c.user["username"], note))
    c.audit("tinjau_peringatan", "weather_alert", aid, {"note": note})
    return {"ok": True}


def _weather_refresh(c: Call):
    c.ctx.collector.trigger()
    return {"ok": True}


def _quake(c: Call):
    r = c.ctx.db.one("SELECT state, checked_at, ok_at, error, detail FROM source_health WHERE source='bmkg_quake'")
    return jsonable(r or {"state": "unknown"})


def _quakes(c: Call):
    col = c.ctx.collector
    if not col.quake_lists:
        col.run_quake_lists()
    return {"lists": col.quake_lists, "note": "Sumber BMKG. Jarak dihitung dari Kota Padang."}


def _forecast(c: Call):
    try:
        return c.ctx.collector.forecast(c.params.get("adm4", ""))
    except ValueError as e:
        raise ApiError(422, str(e))
    except collector.FetchError as e:
        if "HTTP 404" in str(e):
            raise ApiError(404, "BMKG belum menyediakan prakiraan untuk kode wilayah ini.")
        raise ApiError(502, f"Prakiraan BMKG tidak tersedia: {e}")


def _adapter(fn):
    def h(c: Call):
        try:
            return fn(c.ctx.collector)
        except PermissionError as e:
            raise ApiError(404, str(e))
        except (collector.FetchError, tomorrow.TomorrowError) as e:
            raise ApiError(502, f"Sumber tidak tersedia: {e}")
    h.__name__ = fn.__name__
    return h


def _tio_tile(c: Call):
    z, x, y = (int(c.path[k]) for k in ("z", "x", "y"))
    try:
        png = c.ctx.collector.tomorrow.tile(z, x, y)
    except PermissionError as e:
        raise ApiError(404, str(e))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


def _tio_test(c: Call):
    try:
        res = c.ctx.collector.tomorrow.test()
    except tomorrow.TomorrowError as e:
        raise ApiError(502, f"Tomorrow.io: {e}")
    c.audit("tes_tomorrow", "settings", "tomorrow", {"site": res["site"]})
    c.ctx.collector.trigger()
    return res


def _simulate(c: Call):
    on = bool(c.body.get("on"))
    if on:
        c.ctx.collector.start_simulation()
    else:
        c.ctx.collector.stop_simulation()
    c.audit("simulasi_" + ("mulai" if on else "stop"), "weather_alert", "SIMULASI")
    return {"ok": True, "on": on}


# =============================================================== incidents
INCIDENT_COLS = ("id, title, category, lon, lat, occurred_at, severity, status, description, pic, notes, visibility, "
                 "archived, revision, created_by, created_at, updated_by, updated_at")


def _incident_payload(b: dict, cats: set) -> dict:
    d = {"title": s(b.get("title"), 200, "Judul", True, 3),
         "category": s(b.get("category"), 50, "Kategori", True),
         "description": s(b.get("description"), 5000, "Deskripsi"),
         "pic": s(b.get("pic"), 200, "Penanggung jawab"),
         "notes": s(b.get("notes"), 5000, "Catatan"),
         "severity": s(b.get("severity") or "sedang", 20, "Tingkat dampak"),
         "status": s(b.get("status") or "dilaporkan", 20, "Status"),
         "visibility": s(b.get("visibility") or "internal", 20, "Visibilitas"),
         "occurred_at": parse_dt(b.get("occurred_at") or dt.datetime.now(WIB).isoformat(), "Waktu kejadian")}
    need(d["category"] in cats, "Kategori tidak dikenal.")
    need(d["severity"] in SEVERITIES, "Tingkat dampak tidak valid.")
    need(d["status"] in STATUSES, "Status tidak valid.")
    need(d["visibility"] in ("internal", "guest"), "Visibilitas tidak valid.")
    try:
        lon, lat = float(b.get("lon")), float(b.get("lat"))
    except (TypeError, ValueError):
        raise ApiError(422, "Koordinat kejadian wajib diisi.")
    need(90 <= lon <= 145 and -12 <= lat <= 8, "Koordinat di luar wilayah Indonesia.")
    d["lon"], d["lat"] = round(lon, 7), round(lat, 7)
    return d


def _cats(c: Call) -> set:
    return {r["code"] for r in c.ctx.db.all("SELECT code FROM incident_categories")}


def _incidents_list(c: Call):
    cfg = appsettings.get(c.ctx.db)
    where, params = ["true"], []
    if c.role == "guest":
        if not cfg["guest"].get("show_incidents"):
            return {"incidents": []}
        where.append("visibility='guest' AND NOT archived")
    elif c.params.get("archived") != "1":
        where.append("NOT archived")
    for f in ("status", "category", "severity"):
        if c.params.get(f):
            where.append(f"{f} = %s")
            params.append(c.params[f])
    if c.params.get("q"):
        where.append("(title ILIKE %s OR description ILIKE %s)")
        q = "%" + c.params["q"].replace("%", "").replace("_", "")[:100] + "%"
        params += [q, q]
    if c.params.get("from"):
        where.append("occurred_at >= %s")
        params.append(parse_dt(c.params["from"], "Tanggal awal"))
    if c.params.get("to"):
        where.append("occurred_at <= %s")
        params.append(parse_dt(c.params["to"], "Tanggal akhir"))
    cols = INCIDENT_COLS if c.role != "guest" else "id, title, category, lon, lat, occurred_at, severity, status, description, updated_at"
    rows = c.ctx.db.all(
        f"SELECT {cols}, (SELECT count(*) FROM incident_photos p WHERE p.incident_id=i.id AND NOT p.archived"
        + (" AND p.visibility='guest'" if c.role == "guest" else "") + ") AS photo_count, "
        "(SELECT count(*) FROM incident_areas a WHERE a.incident_id=i.id AND NOT a.archived"
        + (" AND a.visibility='guest'" if c.role == "guest" else "") + ") AS area_count "
        f"FROM incidents i WHERE {' AND '.join(where)} ORDER BY occurred_at DESC LIMIT 500", params)
    return {"incidents": jsonable(rows)}


def _incident_get(c: Call):
    iid = int_id(c.path["id"])
    db = c.ctx.db
    guest = c.role == "guest"
    r = db.one(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id=%s", (iid,))
    if not r or (guest and (r["visibility"] != "guest" or r["archived"])):
        raise ApiError(404, "Kejadian tidak ditemukan.")
    if guest:
        for k in ("pic", "notes", "created_by", "updated_by"):
            r.pop(k, None)
    vis = " AND visibility='guest'" if guest else ""
    r["photos"] = db.all("SELECT id, caption, taken_at, uploaded_by, uploaded_at, visibility, width, height, bytes "
                         f"FROM incident_photos WHERE incident_id=%s AND NOT archived{vis} ORDER BY uploaded_at", (iid,))
    r["areas"] = db.all("SELECT id, name, category, color, geometry, area_km2, visibility, revision, updated_at, updated_by "
                        f"FROM incident_areas WHERE incident_id=%s AND NOT archived{vis} ORDER BY id", (iid,))
    an = db.all("SELECT id, computed_at, computed_by, rule_version, published_guest, result FROM impact_analyses "
                "WHERE incident_id=%s" + (" AND published_guest" if guest else "") + " ORDER BY id DESC LIMIT 20", (iid,))
    for a in an:
        res = a.pop("result")
        a["summary"] = {k: res.get(k) for k in ("trafo_count", "gardu_unique_count", "known_customer_count",
                                                "gardu_with_customer_data", "gardu_missing_customer_data",
                                                "customer_total_complete", "capacity_kva_total", "scope_label")}
        if guest:
            a.pop("computed_by", None)
    r["analyses"] = an
    if guest:
        for p in r["photos"]:
            p.pop("uploaded_by", None)
        for a in r["areas"]:
            a.pop("updated_by", None)
    return jsonable(r)


def _notify_incident(c: Call, inc: dict, reason: str):
    cfg = appsettings.get(c.ctx.db)
    wa = cfg["wa"]
    if not (wa.get("enabled") and wa.get("notify_incident")):
        return
    if SEVERITIES.index(inc["severity"]) < SEVERITIES.index(wa.get("incident_min_severity") or "tinggi"):
        return
    when = inc["occurred_at"].astimezone(WIB).strftime("%d/%m/%Y %H:%M WIB") if hasattr(inc["occurred_at"], "astimezone") else inc["occurred_at"]
    msg = (f"*KEJADIAN {reason.upper()} — {inc['title']}*\nKategori: {inc['category']} · Dampak: {inc['severity']} · "
           f"Status: {inc['status']}\nWaktu: {when}\nLokasi: {inc['lat']:.5f}, {inc['lon']:.5f}\n"
           f"{(inc.get('description') or '')[:400]}\nOleh: {c.actor}\n{wa.get('app_url', '')}")
    collector.enqueue(c.ctx.db, cfg, f"inc:{inc['id']}:r{inc['revision']}", msg)


def _incident_create(c: Call):
    d = _incident_payload(c.body, _cats(c))
    db = c.ctx.db
    with db.tx() as t:
        r = t.one("INSERT INTO incidents(title,category,lon,lat,occurred_at,severity,status,description,pic,notes,visibility,"
                  f"created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {INCIDENT_COLS}",
                  (d["title"], d["category"], d["lon"], d["lat"], d["occurred_at"], d["severity"], d["status"],
                   d["description"], d["pic"], d["notes"], d["visibility"], c.actor, c.actor))
        t.run("INSERT INTO incident_revisions(incident_id, revision, data, edited_by) VALUES (%s,1,%s::jsonb,%s)",
              (r["id"], J(jsonable(r)), c.actor))
    c.audit("buat_kejadian", "incident", r["id"], {"title": d["title"]})
    _notify_incident(c, r, "baru")
    return jsonable(r)


def _incident_update(c: Call):
    iid = int_id(c.path["id"])
    rev = c.body.get("revision")
    d = _incident_payload(c.body, _cats(c))
    db = c.ctx.db
    with db.tx() as t:
        cur = t.one(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id=%s FOR UPDATE", (iid,))
        if not cur:
            raise ApiError(404, "Kejadian tidak ditemukan.")
        if str(cur["revision"]) != str(rev):
            raise ApiError(409, f"Kejadian sudah diubah oleh {cur['updated_by']} (revisi {cur['revision']}). "
                                "Muat ulang sebelum menyimpan agar perubahan tidak tertimpa.", {"current": jsonable(cur)})
        r = t.one("UPDATE incidents SET title=%s, category=%s, lon=%s, lat=%s, occurred_at=%s, severity=%s, status=%s, "
                  "description=%s, pic=%s, notes=%s, visibility=%s, revision=revision+1, updated_by=%s, updated_at=now() "
                  f"WHERE id=%s RETURNING {INCIDENT_COLS}",
                  (d["title"], d["category"], d["lon"], d["lat"], d["occurred_at"], d["severity"], d["status"],
                   d["description"], d["pic"], d["notes"], d["visibility"], c.actor, iid))
        t.run("INSERT INTO incident_revisions(incident_id, revision, data, edited_by) VALUES (%s,%s,%s::jsonb,%s)",
              (iid, r["revision"], J(jsonable(r)), c.actor))
    changed = [k for k in d if str(d[k]) != str(cur[k])]
    c.audit("ubah_kejadian", "incident", iid, {"revision": r["revision"], "fields": changed})
    if "status" in changed or "severity" in changed:
        _notify_incident(c, r, f"diperbarui ({r['status']})")
    return jsonable(r)


def _incident_archive(c: Call):
    iid = int_id(c.path["id"])
    arch = bool(c.body.get("archived", True))
    n = c.ctx.db.run("UPDATE incidents SET archived=%s, updated_by=%s, updated_at=now(), revision=revision+1 WHERE id=%s",
                     (arch, c.actor, iid))
    need(n > 0, "Kejadian tidak ditemukan.", 404)
    c.audit("arsip_kejadian" if arch else "pulihkan_kejadian", "incident", iid)
    return {"ok": True}


def _incident_revisions(c: Call):
    iid = int_id(c.path["id"])
    return {"revisions": jsonable(c.ctx.db.all("SELECT revision, data, edited_by, edited_at FROM incident_revisions "
                                               "WHERE incident_id=%s ORDER BY revision DESC", (iid,)))}


# ------------------------------------------------------------------ areas
AREA_COLS = "id, incident_id, name, category, color, geometry, bbox, area_km2, visibility, archived, revision, created_by, created_at, updated_by, updated_at"


def _area_payload(c: Call) -> dict:
    b = c.body
    g = geo.validate_area(b.get("geometry"))
    color = s(b.get("color") or "#0ea5e9", 20, "Warna")
    need(re.fullmatch(r"#[0-9a-fA-F]{6}", color), "Warna harus format #RRGGBB.")
    vis = s(b.get("visibility") or "internal", 20, "Visibilitas")
    need(vis in ("internal", "guest"), "Visibilitas tidak valid.")
    inc = b.get("incident_id")
    if inc not in (None, ""):
        inc = int_id(inc, "ID kejadian")
        need(c.ctx.db.one("SELECT id FROM incidents WHERE id=%s", (inc,)), "Kejadian tidak ditemukan.", 404)
    else:
        inc = None
    return {"name": s(b.get("name"), 200, "Nama area", True, 2), "category": s(b.get("category"), 50, "Kategori") or None,
            "color": color, "geometry": g, "bbox": geo.bbox_of(g), "area_km2": round(geo.area_km2(g), 4),
            "visibility": vis, "incident_id": inc}


def _areas_list(c: Call):
    where, params = ["NOT archived"], []
    if c.role == "guest":
        where.append("visibility='guest'")
    if c.params.get("incident_id"):
        where.append("incident_id=%s")
        params.append(int_id(c.params["incident_id"]))
    rows = c.ctx.db.all(f"SELECT {AREA_COLS} FROM incident_areas WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 500", params)
    if c.role == "guest":
        for r in rows:
            for k in ("created_by", "updated_by"):
                r.pop(k, None)
    return {"areas": jsonable(rows)}


def _area_create(c: Call):
    d = _area_payload(c)
    with c.ctx.db.tx() as t:
        r = t.one("INSERT INTO incident_areas(incident_id,name,category,color,geometry,bbox,area_km2,visibility,created_by,"
                  f"updated_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s) RETURNING {AREA_COLS}",
                  (d["incident_id"], d["name"], d["category"], d["color"], J(d["geometry"]), J(d["bbox"]), d["area_km2"],
                   d["visibility"], c.actor, c.actor))
        t.run("INSERT INTO incident_area_revisions(area_id, revision, name, geometry, edited_by) VALUES (%s,1,%s,%s::jsonb,%s)",
              (r["id"], d["name"], J(d["geometry"]), c.actor))
    c.audit("buat_area", "incident_area", r["id"], {"name": d["name"], "incident_id": d["incident_id"]})
    return jsonable(r)


def _area_update(c: Call):
    aid = int_id(c.path["id"])
    d = _area_payload(c)
    with c.ctx.db.tx() as t:
        cur = t.one("SELECT revision, updated_by FROM incident_areas WHERE id=%s FOR UPDATE", (aid,))
        need(cur, "Area tidak ditemukan.", 404)
        if str(cur["revision"]) != str(c.body.get("revision")):
            raise ApiError(409, f"Area sudah diubah oleh {cur['updated_by']} (revisi {cur['revision']}). Muat ulang dahulu.")
        r = t.one("UPDATE incident_areas SET incident_id=%s, name=%s, category=%s, color=%s, geometry=%s::jsonb, bbox=%s::jsonb, "
                  "area_km2=%s, visibility=%s, revision=revision+1, updated_by=%s, updated_at=now() WHERE id=%s "
                  f"RETURNING {AREA_COLS}",
                  (d["incident_id"], d["name"], d["category"], d["color"], J(d["geometry"]), J(d["bbox"]), d["area_km2"],
                   d["visibility"], c.actor, aid))
        t.run("INSERT INTO incident_area_revisions(area_id, revision, name, geometry, edited_by) VALUES (%s,%s,%s,%s::jsonb,%s)",
              (aid, r["revision"], d["name"], J(d["geometry"]), c.actor))
    c.audit("ubah_area", "incident_area", aid, {"revision": r["revision"]})
    return jsonable(r)


def _area_archive(c: Call):
    aid = int_id(c.path["id"])
    n = c.ctx.db.run("UPDATE incident_areas SET archived=true, updated_by=%s, updated_at=now() WHERE id=%s", (c.actor, aid))
    need(n > 0, "Area tidak ditemukan.", 404)
    c.audit("arsip_area", "incident_area", aid)
    return {"ok": True}


# --------------------------------------------------------------- analysis
def _collect_areas(c: Call) -> tuple[list[dict], list[dict]]:
    b = c.body
    areas, refs = [], []
    ids = b.get("area_ids") or []
    need(isinstance(ids, list) and len(ids) <= 50, "Maksimal 50 area per analisis.")
    for i in ids:
        r = c.ctx.db.one("SELECT id, revision, name, geometry FROM incident_areas WHERE id=%s AND NOT archived", (int_id(i),))
        need(r, f"Area {i} tidak ditemukan.", 404)
        areas.append(r)
        refs.append({"id": r["id"], "revision": r["revision"], "name": r["name"]})
    for j, g in enumerate(b.get("geometries") or []):
        areas.append({"id": None, "revision": None, "name": f"Gambar sementara {j + 1}", "geometry": geo.validate_area(g)})
    need(areas, "Pilih atau gambar minimal satu area.")
    return areas, refs


def _analysis_preview(c: Call):
    areas, _ = _collect_areas(c)
    set_id, counts = analysis.load_customer_counts(c.ctx.db)
    res = analysis.compute(c.ctx.index, areas, counts, set_id)
    res.pop("_all_ids", None)
    res["saved"] = False
    return res


def _analysis_save(c: Call):
    areas, refs = _collect_areas(c)
    need(all(a["id"] for a in areas), "Simpan area terlebih dahulu sebelum menyimpan snapshot analisis.")
    inc = c.body.get("incident_id")
    inc = int_id(inc) if inc not in (None, "") else None
    set_id, counts = analysis.load_customer_counts(c.ctx.db)
    res = analysis.compute(c.ctx.index, areas, counts, set_id)
    all_ids = res.pop("_all_ids")
    aid = analysis.save(c.ctx.db, res, refs, inc, c.actor, all_ids)
    c.audit("simpan_analisis", "impact_analysis", aid, {"areas": refs, "trafo": res["trafo_count"]})
    res.update({"id": aid, "saved": True})
    return res


def _analysis_get(c: Call):
    aid = int_id(c.path["id"])
    r = c.ctx.db.one("SELECT id, incident_id, area_refs, asset_snapshot_id, customer_set_id, rule_version, computed_at, "
                     "computed_by, published_guest, result FROM impact_analyses WHERE id=%s", (aid,))
    need(r, "Analisis tidak ditemukan.", 404)
    if c.role == "guest":
        need(r["published_guest"], "Analisis tidak ditemukan.", 404)
        res = r["result"]
        r["result"] = {k: res.get(k) for k in ("trafo_count", "gardu_unique_count", "known_customer_count",
                                               "gardu_with_customer_data", "gardu_missing_customer_data",
                                               "customer_total_complete", "capacity_kva_total", "scope_label",
                                               "service_state_label", "computed_at", "areas")}
        r.pop("computed_by", None)
    return jsonable(r)


def _analysis_csv(c: Call):
    aid = int_id(c.path["id"])
    r = c.ctx.db.one("SELECT computed_at, result FROM impact_analyses WHERE id=%s", (aid,))
    need(r, "Analisis tidak ditemukan.", 404)
    rows = c.ctx.db.all("SELECT ia.asset_id, ia.known_customer_count, a.name, a.feeder, a.up3, a.ulp, a.gardu_id, a.code, "
                        "a.capacity_kva, a.lon, a.lat FROM impact_analysis_assets ia LEFT JOIN assets a ON a.id=ia.asset_id "
                        "WHERE ia.analysis_id=%s ORDER BY a.up3, a.feeder, a.name", (aid,))
    out = io.StringIO()
    w = csv.writer(out)
    res = r["result"]
    w.writerow([f"Analisis #{aid}", res.get("scope_label"), f"Dihitung {res.get('computed_at')}",
                f"Snapshot aset {res.get('asset_snapshot_id')}", f"Set pelanggan {res.get('customer_snapshot_id')}"])
    w.writerow(["ID_ASET", "NAMA", "PENYULANG", "UP3", "ULP", "LOCATION", "KODE_GARDU", "KVA", "PELANGGAN", "LINTANG", "BUJUR"])
    for x in rows:
        w.writerow([x["asset_id"], x["name"], x["feeder"], x["up3"], x["ulp"], x["gardu_id"], x["code"], x["capacity_kva"],
                    "" if x["known_customer_count"] is None else x["known_customer_count"], x["lat"], x["lon"]])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="analisis-{aid}.csv"'})


def _analysis_publish(c: Call):
    aid = int_id(c.path["id"])
    pub = bool(c.body.get("published"))
    n = c.ctx.db.run("UPDATE impact_analyses SET published_guest=%s WHERE id=%s", (pub, aid))
    need(n > 0, "Analisis tidak ditemukan.", 404)
    c.audit("publikasi_analisis" if pub else "tarik_publikasi_analisis", "impact_analysis", aid)
    return {"ok": True}


# ------------------------------------------------------------------ photos
def _photo_upload(c: Call):
    iid = int_id(c.path["id"])
    need(c.ctx.db.one("SELECT id FROM incidents WHERE id=%s", (iid,)), "Kejadian tidak ditemukan.", 404)
    f = c.form["files"].get("file")
    need(f, "Pilih foto.")
    flds = c.form["fields"]
    vis = flds.get("visibility") or "internal"
    need(vis in ("internal", "guest"), "Visibilitas tidak valid.")
    meta = photos.store(c.ctx.settings.data_dir, f[1], c.ctx.settings.max_photo_mb)
    taken = parse_dt(flds["taken_at"], "Waktu pengambilan") if flds.get("taken_at") else meta["taken_at"]
    r = c.ctx.db.one("INSERT INTO incident_photos(incident_id, original_name, object_key, thumb_key, content_type, bytes, sha256,"
                     " width, height, caption, taken_at, uploaded_by, visibility) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                     " RETURNING id, caption, taken_at, uploaded_by, uploaded_at, visibility, width, height, bytes",
                     (iid, Path(f[0]).name[:200], meta["object_key"], meta["thumb_key"], meta["content_type"], meta["bytes"],
                      meta["sha256"], meta["width"], meta["height"], s(flds.get("caption"), 500, "Keterangan"), taken,
                      c.actor, vis))
    c.audit("unggah_foto", "incident_photo", r["id"], {"incident_id": iid, "bytes": meta["bytes"]})
    return jsonable(r)


def _photo_file(thumb: bool):
    def h(c: Call):
        pid = int_id(c.path["id"])
        r = c.ctx.db.one("SELECT p.object_key, p.thumb_key, p.content_type, p.visibility, p.archived, i.visibility AS ivis, "
                         "i.archived AS iarch FROM incident_photos p JOIN incidents i ON i.id=p.incident_id WHERE p.id=%s", (pid,))
        if not r or (c.role == "guest" and (r["visibility"] != "guest" or r["ivis"] != "guest" or r["archived"] or r["iarch"])):
            raise ApiError(404, "Foto tidak ditemukan.")
        path = photos.resolve(c.ctx.settings.data_dir, r["thumb_key"] if thumb else r["object_key"])
        if not path.exists():
            raise ApiError(404, "Berkas foto hilang dari penyimpanan.")
        return FileResponse(path, media_type="image/jpeg" if thumb else r["content_type"],
                            headers={"Cache-Control": "private, max-age=86400"})
    h.__name__ = "photo_thumb" if thumb else "photo_file"
    return h


def _photo_update(c: Call):
    pid = int_id(c.path["id"])
    b = c.body
    vis = b.get("visibility") or "internal"
    need(vis in ("internal", "guest"), "Visibilitas tidak valid.")
    n = c.ctx.db.run("UPDATE incident_photos SET caption=%s, visibility=%s, archived=%s WHERE id=%s",
                     (s(b.get("caption"), 500, "Keterangan"), vis, bool(b.get("archived")), pid))
    need(n > 0, "Foto tidak ditemukan.", 404)
    c.audit("ubah_foto", "incident_photo", pid, {"visibility": vis, "archived": bool(b.get("archived"))})
    return {"ok": True}


# =================================================================== admin
def _users(c: Call):
    return {"users": jsonable(c.ctx.db.all("SELECT id, username, display_name, role, active, phone, must_change_password, "
                                           "created_at, last_login_at FROM users ORDER BY username"))}


def _user_create(c: Call):
    b = c.body
    username = s(b.get("username"), 64, "Nama pengguna", True, 3).lower()
    need(re.fullmatch(r"[a-z0-9._-]{3,64}", username), "Nama pengguna hanya huruf kecil, angka, titik, garis bawah/strip.")
    role = b.get("role")
    need(role in ("admin", "user"), "Peran harus admin atau user.")
    try:
        h = security.hash_password(str(b.get("password") or ""))
    except ValueError as e:
        raise ApiError(422, str(e))
    if c.ctx.db.one("SELECT id FROM users WHERE lower(username)=%s", (username,)):
        raise ApiError(409, "Nama pengguna sudah dipakai.")
    r = c.ctx.db.one("INSERT INTO users(username, display_name, role, password_hash, phone, must_change_password) "
                     "VALUES (%s,%s,%s,%s,%s,true) RETURNING id",
                     (username, s(b.get("display_name"), 100, "Nama tampilan"), role, h, s(b.get("phone"), 30, "No. HP") or None))
    c.audit("buat_akun", "user", r["id"], {"username": username, "role": role})
    return {"ok": True, "id": r["id"]}


def _user_update(c: Call):
    uid = int_id(c.path["id"])
    b = c.body
    db = c.ctx.db
    cur = db.one("SELECT id, role, active FROM users WHERE id=%s", (uid,))
    need(cur, "Akun tidak ditemukan.", 404)
    role = b.get("role", cur["role"])
    active = bool(b.get("active", cur["active"]))
    need(role in ("admin", "user"), "Peran tidak valid.")
    if cur["role"] == "admin" and (role != "admin" or not active):
        n = db.one("SELECT count(*) AS n FROM users WHERE role='admin' AND active AND id<>%s", (uid,))["n"]
        need(n > 0, "Tidak dapat menurunkan/menonaktifkan admin terakhir.")
    db.run("UPDATE users SET display_name=%s, role=%s, active=%s, phone=%s WHERE id=%s",
           (s(b.get("display_name"), 100, "Nama tampilan"), role, active, s(b.get("phone"), 30, "No. HP") or None, uid))
    if b.get("password"):
        try:
            h = security.hash_password(str(b["password"]))
        except ValueError as e:
            raise ApiError(422, str(e))
        db.run("UPDATE users SET password_hash=%s, must_change_password=true WHERE id=%s", (h, uid))
        db.run("DELETE FROM sessions WHERE user_id=%s", (uid,))
    if not active:
        db.run("DELETE FROM sessions WHERE user_id=%s", (uid,))
    c.audit("ubah_akun", "user", uid, {"role": role, "active": active, "reset_password": bool(b.get("password"))})
    return {"ok": True}


def _audit_list(c: Call):
    lim = min(int(c.params.get("limit", 200)), 1000)
    return {"events": jsonable(c.ctx.db.all("SELECT id, at, actor_name, action, entity_type, entity_id, detail, ip "
                                            "FROM audit_events ORDER BY id DESC LIMIT %s", (lim,)))}


def _settings_get(c: Call):
    return appsettings.masked(appsettings.get(c.ctx.db))


def _settings_put(c: Call):
    allowed = {"corridor_m", "basemaps", "guest", "openmeteo", "rainviewer", "tomorrow", "windy", "wa"}
    patch = {k: v for k, v in c.body.items() if k in allowed}
    if "corridor_m" in patch:
        try:
            patch["corridor_m"] = max(0, min(2000, float(patch["corridor_m"])))
        except (TypeError, ValueError):
            raise ApiError(422, "Koridor harus angka meter.")
    if "wa" in patch and patch["wa"].get("url"):
        need(re.match(r"^https?://", patch["wa"]["url"]), "URL WA gateway harus diawali http:// atau https://")
    if isinstance(patch.get("tomorrow"), dict):
        t = patch["tomorrow"]
        try:
            t["interval_min"] = max(30, min(360, int(t.get("interval_min") or 60)))
            t["max_points"] = max(1, min(15, int(t.get("max_points") or 8)))
        except (TypeError, ValueError):
            raise ApiError(422, "Interval dan jumlah lokasi Tomorrow.io harus angka.")
        per_run = t["max_points"] + (len(tomorrow.tiles_for()) if t.get("rain_tiles", True) else 0)
        per_hour = per_run * 60.0 / t["interval_min"]
        need(per_hour <= tomorrow.HOUR_LIMIT and per_hour * 24 <= tomorrow.DAY_LIMIT,
             f"Pengaturan ini memakai ±{per_hour:.0f} panggilan/jam ({per_hour * 24:.0f}/hari), melebihi kuota gratis "
             "Tomorrow.io (25/jam, 500/hari). Kurangi jumlah lokasi atau perpanjang interval.")
    if "basemaps" in patch:
        for bm in patch["basemaps"]:
            need(re.match(r"^https?://", str(bm.get("url", ""))), "URL basemap tidak valid.")
    old_tio = appsettings.get(c.ctx.db)["tomorrow"]
    new = appsettings.put(c.ctx.db, patch, c.actor)
    c.ctx.index.layer_cache = {}
    if "corridor_m" in patch:
        c.ctx.collector.trigger()
    if any(old_tio.get(k) != new["tomorrow"].get(k) for k in ("enabled", "api_key", "max_points", "rain_tiles")):
        # Ambil ulang hanya bila sumber berubah, supaya kuota gratis tidak terbuang.
        c.ctx.collector.tomorrow.paused_until = 0
        c.ctx.collector.tomorrow.points_at = 0
        c.ctx.collector.tomorrow.tiles_at = 0
        c.ctx.collector.trigger()
    c.audit("ubah_pengaturan", "settings", "config", {"keys": list(patch)})
    return appsettings.masked(new)


def _notifications(c: Call):
    return {"notifications": jsonable(c.ctx.db.all("SELECT id, channel, target, left(message, 500) AS message, status, attempts, "
                                                   "created_at, sent_at, error FROM notifications ORDER BY id DESC LIMIT 100"))}


def _notification_test(c: Call):
    cfg = appsettings.get(c.ctx.db)
    target = s(c.body.get("target"), 40, "Nomor/ID tujuan", True)
    need(cfg["wa"].get("url"), "Isi URL WA gateway terlebih dahulu.")
    try:
        status, text = collector.send_wa(cfg["wa"], target, "Tes notifikasi Disaster Information Center DCC — "
                                         + dt.datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB"))
    except collector.FetchError as e:
        raise ApiError(502, f"WA gateway tidak dapat dihubungi: {e}")
    c.audit("tes_notifikasi", "notification", None, {"target": target, "status": status})
    return {"status": status, "response": text[:500]}


# ================================================================== routes
def index_page(request: Request):
    # Tambahkan ?v=<waktu ubah berkas> agar browser selalu memuat app.js/app.css terbaru setelah pembaruan.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        ver = int((STATIC_DIR / name).stat().st_mtime)
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={ver}"')
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


def build_routes():
    R = lambda path, fn, methods=("GET",), **kw: Route(path, api(fn, **kw), methods=list(methods))
    mb = lambda mb: mb * 1024 * 1024 + 65536
    return [
        Route("/", index_page),
        R("/api/auth/login", _login, ("POST",), body="json"),
        R("/api/auth/logout", _logout, ("POST",)),
        R("/api/me", _me),
        R("/api/me/password", _change_password, ("POST",), role="user", body="json"),
        R("/api/config", _config),
        R("/api/health", _health),
        R("/api/assets/summary", _asset_summary),
        R("/api/assets/layer/{kind}", _asset_layer),
        R("/api/assets/search", _asset_search),
        R("/api/assets/{id:int}", _asset_get),
        R("/api/feeders", _feeders, role="user"),
        R("/api/weather/alerts", _alerts),
        R("/api/weather/alerts/{id}/assets", _alert_assets, role="user"),
        R("/api/weather/alerts/{id}/review", _alert_review, ("POST",), role="user", body="json"),
        R("/api/weather/refresh", _weather_refresh, ("POST",), role="user"),
        R("/api/weather/quake", _quake),
        R("/api/weather/quakes", _quakes),
        R("/api/weather/forecast", _forecast, role="user"),
        R("/api/weather/wind", _adapter(lambda col: col.wind_grid())),
        R("/api/weather/aqi", _adapter(lambda col: col.aqi_points())),
        R("/api/weather/radar", _adapter(lambda col: col.radar_meta())),
        R("/api/weather/tio", _adapter(lambda col: col.tomorrow.view())),
        R("/api/weather/tio/tile/{z:int}/{x:int}/{y:int}.png", _tio_tile),
        R("/api/weather/simulate", _simulate, ("POST",), role="user", body="json"),
        R("/api/incidents", _incidents_list),
        R("/api/incidents", _incident_create, ("POST",), role="user", body="json"),
        R("/api/incidents/{id:int}", _incident_get),
        R("/api/incidents/{id:int}", _incident_update, ("PUT",), role="user", body="json"),
        R("/api/incidents/{id:int}/archive", _incident_archive, ("POST",), role="user", body="json"),
        R("/api/incidents/{id:int}/revisions", _incident_revisions, role="user"),
        R("/api/incidents/{id:int}/photos", _photo_upload, ("POST",), role="user", body="form", max_body=mb(10)),
        R("/api/photos/{id:int}/thumb", _photo_file(True)),
        R("/api/photos/{id:int}/file", _photo_file(False)),
        R("/api/photos/{id:int}", _photo_update, ("PUT",), role="user", body="json"),
        R("/api/areas", _areas_list),
        R("/api/areas", _area_create, ("POST",), role="user", body="json"),
        R("/api/areas/{id:int}", _area_update, ("PUT",), role="user", body="json"),
        R("/api/areas/{id:int}/archive", _area_archive, ("POST",), role="user", body="json"),
        R("/api/analysis/preview", _analysis_preview, ("POST",), role="user", body="json"),
        R("/api/analysis", _analysis_save, ("POST",), role="user", body="json"),
        R("/api/analysis/{id:int}", _analysis_get),
        R("/api/analysis/{id:int}/csv", _analysis_csv, role="user"),
        R("/api/analysis/{id:int}/publish", _analysis_publish, ("POST",), role="user", body="json"),
        R("/api/admin/import/kml", _import_upload, ("POST",), role="admin", body="form", max_body=mb(150)),
        R("/api/admin/import/server-files", _import_server_list, role="admin"),
        R("/api/admin/import/server", _import_server, ("POST",), role="admin", body="json"),
        R("/api/admin/import/status", _import_status, role="admin"),
        R("/api/admin/snapshots", _snapshots, role="admin"),
        R("/api/admin/customers/template.csv", _customer_template, role="user"),
        R("/api/admin/customers/upload", _customer_upload, ("POST",), role="admin", body="form", max_body=mb(30)),
        R("/api/admin/customers/sets", _customer_sets, role="admin"),
        R("/api/admin/users", _users, role="admin"),
        R("/api/admin/users", _user_create, ("POST",), role="admin", body="json"),
        R("/api/admin/users/{id:int}", _user_update, ("PUT",), role="admin", body="json"),
        R("/api/admin/audit", _audit_list, role="admin"),
        R("/api/admin/settings", _settings_get, role="admin"),
        R("/api/admin/settings", _settings_put, ("PUT",), role="admin", body="json"),
        R("/api/admin/tomorrow/test", _tio_test, ("POST",), role="admin"),
        R("/api/admin/notifications", _notifications, role="admin"),
        R("/api/admin/notifications/test", _notification_test, ("POST",), role="admin", body="json"),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]


def create_app(settings: Settings | None = None, db: Database | None = None, start_collector: bool | None = None) -> Starlette:
    settings = settings or load_settings()
    db = db or Database(settings.database_url)
    ctx = Ctx(settings, db)
    run_collector = settings.collector_enabled if start_collector is None else start_collector

    @contextlib.asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Saat PC baru menyala, layanan PostgreSQL bisa belum siap: tunggu sampai 10 menit.
        for attempt in range(120):
            try:
                await asyncio.to_thread(db.migrate)
                break
            except Exception as exc:  # pragma: no cover
                if attempt == 119:
                    raise
                log.warning("database belum siap (%s); mencoba lagi dalam 5 detik", str(exc).splitlines()[0][:200])
                await asyncio.sleep(5)
        gen = await asyncio.to_thread(security.ensure_initial_admin, db, settings.initial_admin_user,
                                      settings.initial_admin_password)
        if gen:
            msg = (f"Akun admin awal dibuat: {settings.initial_admin_user} / {gen} "
                   "(wajib diganti saat login pertama)")
            log.warning(msg)
            (settings.data_dir / "ADMIN-AWAL.txt").write_text(msg + "\nHapus berkas ini setelah login.\n", encoding="utf-8")
        await asyncio.to_thread(ctx.index.load, db)
        log.info("indeks aset dimuat: %s", ctx.index.counts())
        if run_collector:
            ctx.collector.start()
        yield
        await ctx.collector.stop()
        db.close()

    app = Starlette(routes=build_routes(), lifespan=lifespan,
                    middleware=[Middleware(NetworkGuard, settings=settings), Middleware(GZipMiddleware, minimum_size=1024)])
    app.state.ctx = ctx
    return app
