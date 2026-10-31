"""Background collectors: BMKG CAP/gempa, exposure, notifications and optional weather adapters.

Berjalan di proses server (tetap bekerja walau browser ditutup). Hanya satu
instans yang menjadi collector berkat pg advisory lock, sehingga kuota BMKG
dipakai bersama oleh semua pengguna.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import appsettings, cap, geo
from .db import J

log = logging.getLogger("dcc.collector")

BMKG_FEED = "https://www.bmkg.go.id/alerts/nowcast/id"
BMKG_CAP_BASE = "https://www.bmkg.go.id/alerts/nowcast/id/"
BMKG_QUAKE = "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json"
BMKG_QUAKE_LISTS = {"dirasakan": "https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json",
                    "m5": "https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json"}
PADANG = (-0.95, 100.35)
OM_TTL = 10800  # detik; Open-Meteo memperbarui model per jam, deret 48 jam disimpan 3 jam
BMKG_FORECAST = "https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4={}"
UA = "DCC-DisasterCenter/1.0 (+intranet PLN UP2D Sumbar)"
SUMBAR_BBOX = (98.5, -3.6, 101.95, 0.95)
EXPOSE_POINT_KINDS = ("trafo", "gi", "gh", "recloser", "lbs", "pltd")


class FetchError(Exception):
    pass


# Penghitung permintaan keluar per sumber (hari UTC berjalan), untuk analisa pemakaian API di Admin.
USAGE: dict = {}
_usage_lock = threading.Lock()


def count(source: str, n: int = 1, cached: bool = False):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    with _usage_lock:
        u = USAGE.setdefault(source, {"day": day, "requests": 0, "cache_hits": 0})
        if u["day"] != day:
            u.update(day=day, requests=0, cache_hits=0)
        u["cache_hits" if cached else "requests"] += n


def http_get(url: str, timeout: float = 15.0, max_bytes: int = 8_000_000, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}")
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError("Ukuran respons melebihi batas")
            return data
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Koneksi gagal: {getattr(exc, 'reason', exc)}") from exc


def http_post(url: str, body: bytes, headers: dict, timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode("utf-8", "replace") if exc.fp else str(exc)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Koneksi gagal: {getattr(exc, 'reason', exc)}") from exc


def set_health(db, source: str, state: str, error: str | None = None, detail: dict | None = None) -> None:
    db.run("INSERT INTO source_health(source, state, checked_at, ok_at, error, detail) VALUES "
           "(%s, %s, now(), CASE WHEN %s::text IN ('ok','partial') THEN now() END, %s, %s::jsonb) "
           "ON CONFLICT (source) DO UPDATE SET state=EXCLUDED.state, checked_at=now(), "
           "ok_at=COALESCE(EXCLUDED.ok_at, source_health.ok_at), error=EXCLUDED.error, "
           "detail=COALESCE(EXCLUDED.detail, source_health.detail)",
           (source, state, state, error, J(detail) if detail is not None else None))


def warning_from_row(r: dict) -> cap.Warning:
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else (v or "")
    return cap.Warning(id=r["id"], title=r["title"] or "", description=r["description"] or "",
                       instruction=r["instruction"] or "", severity=r["severity"] or "", certainty=r["certainty"] or "",
                       urgency=r["urgency"] or "", sent=iso(r["sent"]), effective=iso(r["effective"]),
                       expires=iso(r["expires"]), status=r["status"] or "", msg_type=r["msg_type"] or "",
                       references=r["refs"] or [], areas=r["areas"] or [], areas_text=r["areas_text"] or [])


def compute_exposure(index, warning: cap.Warning, corridor_m: float) -> list[int]:
    ids = set()
    for area in warning.areas:
        try:
            bb = geo.bbox_of(area)
        except geo.GeometryError:
            continue
        pad = geo.meters_to_deg(corridor_m, bb[1]) * 1.2
        pbb = (bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad)
        for r in index.points_in_bbox(bb, kinds=EXPOSE_POINT_KINDS):
            if geo.covers_point(area, (r["lon"], r["lat"])):
                ids.add(r["id"])
        for r in index.by_kind.get("jtm", []):
            if r["id"] in ids or not geo.bbox_intersects(r["bbox"], pbb):
                continue
            if geo.geom_intersects_area(r["geometry"], area, corridor_m):
                ids.add(r["id"])
    return sorted(ids)


class Collector:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock_conn = None
        self.is_leader = False
        self.forecast_cache: dict = {}
        self.adapter_cache: dict = {}
        self.quake_lists: dict = {}
        self.simulation: cap.Warning | None = None
        self.simulation_exposure: list[int] = []
        self._run_lock = threading.Lock()
        from .tomorrow import Tomorrow  # impor lambat: tomorrow memakai set_health dari modul ini
        self.tomorrow = Tomorrow(ctx)

    # ------------------------------------------------------- lifecycle
    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stop.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            except Exception:
                pass

    def trigger(self):
        self._wake.set()

    def _try_leader(self) -> bool:
        if self.is_leader:
            return True
        try:
            if self._lock_conn is None:
                self._lock_conn = self.db._connect(self.db.dsn)
            cur = self._lock_conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(814504) AS ok")
            row = cur.fetchone()
            self._lock_conn.commit()
            self.is_leader = bool(row and (row["ok"] if isinstance(row, dict) else row[0]))
        except Exception as exc:  # pragma: no cover
            log.warning("advisory lock gagal: %s", exc)
            self.is_leader = False
            self._lock_conn = None
        return self.is_leader

    async def _loop(self):
        last_quake = 0.0
        interval = self.ctx.settings.bmkg_interval_seconds
        while not self._stop.is_set():
            try:
                if await asyncio.to_thread(self._try_leader):
                    await asyncio.to_thread(self.run_cap_cycle)
                    if time.time() - last_quake > 300:
                        await asyncio.to_thread(self.run_quake)
                        last_quake = time.time()
                    await asyncio.to_thread(self.process_notifications)
                    try:
                        await asyncio.to_thread(self.tomorrow.refresh)
                    except Exception as exc:  # pragma: no cover
                        log.warning("refresh Tomorrow.io gagal: %s", exc)
            except Exception as exc:  # pragma: no cover
                log.exception("siklus collector gagal: %s", exc)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------- CAP
    def run_cap_cycle(self, feed_text: str | None = None, cap_texts: dict | None = None) -> dict:
        """Fetch feed + CAP details, store, recompute exposure. Test hooks allow injected texts."""
        with self._run_lock:
            return self._run_cap_cycle(feed_text, cap_texts)

    def _run_cap_cycle(self, feed_text, cap_texts):
        db = self.db
        try:
            if feed_text is None:
                feed_text = http_get(BMKG_FEED, accept="application/rss+xml,application/xml").decode("utf-8", "replace")
                count("bmkg")
            items = cap.feed_items(feed_text)
        except (FetchError, cap.CapError) as exc:
            set_health(db, "bmkg_cap", "error", str(exc))
            self._recompute_exposures()
            return {"state": "error", "error": str(exc)}
        links = []
        for it in items:
            u = urllib.parse.urlparse(urllib.parse.urljoin("https://www.bmkg.go.id", it["link"]))
            if u.scheme == "https" and u.netloc == "www.bmkg.go.id" and u.path.startswith("/alerts/nowcast/id/"):
                fname = u.path.rsplit("/", 1)[-1]
                if re.fullmatch(r"[A-Za-z0-9_-]{1,180}\.xml", fname) and fname not in links:
                    links.append(fname)
        ok, failed, skipped = 0, [], 0
        known = {r["source_link"] for r in db.all("SELECT source_link FROM weather_alerts WHERE source_link = ANY(%s::text[])",
                                                   ("{" + ",".join('"' + BMKG_CAP_BASE + f + '"' for f in links[:25]) + "}",))} \
            if links and cap_texts is None else set()
        for fname in links[:25]:
            if BMKG_CAP_BASE + fname in known:
                # Berkas CAP tidak berubah setelah terbit (pembaruan memakai ID baru), jadi tidak diunduh ulang.
                skipped += 1
                ok += 1
                count("bmkg", cached=True)
                continue
            try:
                text = (cap_texts or {}).get(fname)
                if text is None:
                    text = http_get(BMKG_CAP_BASE + fname, accept="application/xml").decode("utf-8", "replace")
                    count("bmkg")
                w = cap.parse_cap(text)
                self._store_warning(w, BMKG_CAP_BASE + fname)
                ok += 1
            except (FetchError, cap.CapError) as exc:
                failed.append(f"{fname}: {exc}")
        state = "ok" if not failed else ("partial" if ok else "error")
        set_health(db, "bmkg_cap", state, "; ".join(failed)[:1000] or None,
                   {"items_sumbar": len(items), "cap_ok": ok, "cap_failed": len(failed), "cap_cached": skipped})
        n = self._recompute_exposures()
        return {"state": state, "items": len(items), "cap_ok": ok, "failed": failed, "active": n}

    def _store_warning(self, w: cap.Warning, link: str):
        to_ts = lambda s: s or None
        self.db.run(
            "INSERT INTO weather_alerts(id,sent,effective,expires,status,msg_type,refs,title,description,instruction,"
            "severity,certainty,urgency,areas,areas_text,source_link) VALUES (%s,%s::timestamptz,%s::timestamptz,%s::timestamptz,"
            "%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,"
            "%s::jsonb,%s::jsonb,%s) ON CONFLICT (id) DO UPDATE SET fetched_at = now()",
            (w.id, to_ts(w.sent), to_ts(w.effective), to_ts(w.expires), w.status, w.msg_type, J(w.references), w.title,
             w.description, w.instruction, w.severity, w.certainty, w.urgency, J(w.areas), J(w.areas_text), link))

    def active_alerts(self) -> list[cap.Warning]:
        rows = self.db.all("SELECT * FROM weather_alerts WHERE sent > now() - interval '4 days' ORDER BY sent")
        return cap.active_warnings([warning_from_row(r) for r in rows])

    def _recompute_exposures(self) -> int:
        cfg = appsettings.get(self.db)
        corridor = float(cfg.get("corridor_m") or 0)
        active = self.active_alerts()
        index = self.ctx.index
        for w in active:
            ids = compute_exposure(index, w, corridor)
            with self.db.tx() as t:
                t.run("DELETE FROM alert_exposures WHERE alert_id = %s", (w.id,))
                for s in range(0, len(ids), 1000):
                    chunk = ids[s:s + 1000]
                    t.run("INSERT INTO alert_exposures(alert_id, asset_id) VALUES " + ",".join(["(%s,%s)"] * len(chunk)),
                          [x for a in chunk for x in (w.id, a)])
            if cfg["wa"].get("enabled") and cfg["wa"].get("notify_bmkg"):
                self._enqueue_alert(cfg, w, ids)
        if self.simulation:
            self.simulation_exposure = compute_exposure(index, self.simulation, corridor)
        return len(active)

    def _enqueue_alert(self, cfg, w: cap.Warning, ids: list[int]):
        index = self.ctx.index
        trafo = [index.by_id[i] for i in ids if i in index.by_id and index.by_id[i]["kind"] == "trafo"]
        lines = [index.by_id[i] for i in ids if i in index.by_id and index.by_id[i]["kind"] == "jtm"]
        feeders = sorted({r["feeder"] for r in trafo if r["feeder"]} | {r["name"] for r in lines})
        exp = cap.parse_time(w.expires)
        exp_txt = dt.datetime.fromtimestamp(exp, dt.timezone(dt.timedelta(hours=7))).strftime("%d/%m %H:%M WIB") if exp else "-"
        msg = (f"*PERINGATAN BMKG — {w.title}*\n"
               f"Berlaku s.d. {exp_txt}\nWilayah: {', '.join(w.areas_text)[:400]}\n"
               f"Paparan aset: {len(trafo)} gardu/trafo, {len(lines)} penyulang JTM.\n"
               + (f"Penyulang: {', '.join(feeders[:15])}{' …' if len(feeders) > 15 else ''}\n" if feeders else "")
               + f"Sumber: BMKG (CAP {w.id}). Verifikasi lapangan sesuai SOP.\n{cfg['wa'].get('app_url', '')}")
        # Satu pesan WA per isi peringatan: penerbitan ulang BMKG dengan ID baru tidak dikirim lagi.
        for prev in w.republished:
            if self.db.one("SELECT 1 AS x FROM notifications WHERE dedupe_key LIKE %s LIMIT 1", (f"cap:{prev}:%",)):
                return
        enqueue(self.db, cfg, f"cap:{w.id}", msg)

    # ---------------------------------------------------------- quake
    def run_quake_lists(self, texts: dict | None = None) -> dict:
        """Daftar 15 gempa dirasakan dan 15 gempa M5+ terakhir (BMKG), disimpan di memori."""
        for key, url in BMKG_QUAKE_LISTS.items():
            try:
                raw = (texts or {}).get(key)
                if raw is None:
                    raw = http_get(url, accept="application/json").decode("utf-8", "replace")
                    count("bmkg")
                self.quake_lists[key] = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "items": [quake_item(g) for g in json.loads(raw)["Infogempa"]["gempa"]][:15]}
            except (FetchError, KeyError, ValueError, TypeError) as exc:
                log.warning("daftar gempa %s gagal: %s", key, exc)
        return self.quake_lists

    def run_quake(self, text: str | None = None, text_lists: dict | None = None) -> dict | None:
        try:
            if text is None:
                text = http_get(BMKG_QUAKE, accept="application/json").decode("utf-8", "replace")
                count("bmkg")
            data = json.loads(text)["Infogempa"]["gempa"]
            lat, lon = (float(v) for v in data["Coordinates"].split(",")[:2])
            q = {"datetime": data.get("DateTime"), "tanggal": data.get("Tanggal"), "jam": data.get("Jam"),
                 "magnitude": data.get("Magnitude"), "kedalaman": data.get("Kedalaman"), "wilayah": data.get("Wilayah"),
                 "potensi": data.get("Potensi"), "dirasakan": data.get("Dirasakan"), "lat": lat, "lon": lon,
                 "shakemap": data.get("Shakemap")}
            set_health(self.db, "bmkg_quake", "ok", None, q)
            if text is None:  # mode langsung; pengujian menyuntik teks tanpa jaringan
                self.run_quake_lists(text_lists)
            return q
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            set_health(self.db, "bmkg_quake", "error", str(exc)[:500])
            return None

    # ------------------------------------------------------- forecast
    def _cache_dir(self, sub: str):
        st = getattr(self.ctx, "settings", None)
        if st is None:
            return None
        d = Path(st.data_dir) / "cache" / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _disk_get(self, sub: str, name: str, ttl: float):
        d = self._cache_dir(sub)
        f = d / name if d else None
        if f and f.exists():
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if time.time() - rec["at"] < ttl:
                    return rec
            except (ValueError, KeyError, OSError):
                pass
        return None

    def _disk_put(self, sub: str, name: str, data) -> dict:
        rec = {"at": time.time(), "data": data}
        d = self._cache_dir(sub)
        if d:
            try:
                tmp = d / (name + ".tmp")
                tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
                tmp.replace(d / name)
            except OSError as exc:  # pragma: no cover
                log.warning("cache %s/%s gagal disimpan: %s", sub, name, exc)
        return rec

    def forecast(self, adm4: str) -> dict:
        """Prakiraan BMKG 3 harian per desa. BMKG memperbarui 2 kali sehari; cache disk 3 jam."""
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{4}", adm4 or ""):
            raise ValueError("Kode ADM4 harus berformat 13.71.01.1001")
        hit = self.forecast_cache.get(adm4)
        if hit and time.time() - hit[0] < 10800:
            count("bmkg", cached=True)
            return hit[1]
        rec = self._disk_get("bmkg_forecast", adm4 + ".json", 10800)
        if rec is None:
            data = json.loads(http_get(BMKG_FORECAST.format(adm4), accept="application/json"))
            count("bmkg")
            rec = self._disk_put("bmkg_forecast", adm4 + ".json", data)
        else:
            count("bmkg", cached=True)
        self.forecast_cache[adm4] = (rec["at"], rec["data"])
        if len(self.forecast_cache) > 300:
            self.forecast_cache.pop(next(iter(self.forecast_cache)))
        return rec["data"]

    # ------------------------------------------------ optional adapters
    def _cached(self, key, ttl, fn):
        hit = self.adapter_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self.adapter_cache[key] = (time.time(), val)
        return val

    def _om_series(self, key: str, fetch, n_points: int) -> dict:
        """Deret per jam Open-Meteo (48 jam) disimpan di disk 3 jam dan dipakai untuk jam mana pun di dalamnya.

        Open-Meteo menghitung setiap lokasi sebagai satu panggilan; dengan cara ini grid angin (±130 lokasi)
        cukup diambil 8 kali sehari, bukan setiap kali lapisan dibuka.
        """
        mem = self.adapter_cache.get(key)
        if mem and time.time() - mem[0] < OM_TTL:
            count("openmeteo", n_points, cached=True)
            return mem[1]
        rec = self._disk_get("openmeteo", key + ".json", OM_TTL)
        if rec is None:
            rec = self._disk_put("openmeteo", key + ".json", fetch())
            count("openmeteo", n_points)
        else:
            count("openmeteo", n_points, cached=True)
        self.adapter_cache[key] = (rec["at"], rec)
        return rec

    @staticmethod
    def _hour_index(times: list, at: float) -> int:
        target = dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:00")
        if target in times:
            return times.index(target)
        raise FetchError("Data tersimpan tidak mencakup jam yang diminta")

    def wind_grid(self, offset_h: int = 0) -> dict:
        cfg = appsettings.get(self.db)["openmeteo"]
        if not cfg.get("enabled"):
            raise PermissionError("Adapter Open-Meteo belum diaktifkan Admin.")
        lats, lons = [], []
        lat = SUMBAR_BBOX[1] + 0.2
        while lat <= SUMBAR_BBOX[3]:
            lon = SUMBAR_BBOX[0] + 0.2
            while lon <= SUMBAR_BBOX[2]:
                lats.append(round(lat, 2))
                lons.append(round(lon, 2))
                lon += 0.35
            lat += 0.35

        def fetch():
            host = "customer-api.open-meteo.com" if cfg.get("api_key") else "api.open-meteo.com"
            q = {"latitude": ",".join(map(str, lats)), "longitude": ",".join(map(str, lons)),
                 "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,cape",
                 "wind_speed_unit": "kmh", "timezone": "UTC", "past_hours": 1, "forecast_hours": 48}
            if cfg.get("api_key"):
                q["apikey"] = cfg["api_key"]
            data = json.loads(http_get(f"https://{host}/v1/forecast?" + urllib.parse.urlencode(q), timeout=40,
                                       max_bytes=20_000_000))
            data = data if isinstance(data, list) else [data]
            return [{"lat": d.get("latitude"), "lon": d.get("longitude"), "hourly": d.get("hourly") or {}} for d in data]

        rec = self._om_series("wind", fetch, len(lats))
        at = time.time() + 3600 * offset_h
        pts = []
        for d in rec["data"]:
            h = d["hourly"]
            try:
                k = self._hour_index(h.get("time") or [], at)
            except FetchError:
                continue
            g = lambda name: (h.get(name) or [None] * (k + 1))[k]
            pts.append({"lat": d["lat"], "lon": d["lon"], "ws": g("wind_speed_10m"), "wd": g("wind_direction_10m"),
                        "wg": g("wind_gusts_10m"), "rain": g("precipitation"), "cape": g("cape"), "time": h["time"][k]})
        if not pts:
            raise FetchError("Data angin tersimpan sudah tidak mencakup jam ini")
        return {"source": "Open-Meteo (model NWP)", "level": "10 m di atas permukaan", "unit": "km/j",
                "direction_note": "Arah panah = arah angin bertiup (menuju). Nilai derajat = arah datang angin.",
                "cape_note": "CAPE adalah indikasi energi konvektif model, BUKAN deteksi awan Cb.",
                "fetched_at": dt.datetime.fromtimestamp(rec["at"], dt.timezone.utc).isoformat(),
                "offset_h": offset_h, "points": pts}

    def aqi_points(self, offset_h: int = 0) -> dict:
        cfg = appsettings.get(self.db)["openmeteo"]
        if not cfg.get("enabled"):
            raise PermissionError("Adapter Open-Meteo belum diaktifkan Admin.")
        index = self.ctx.index
        offices = [r for r in index.by_kind.get("kantor", []) if re.match(r"^(UP3|ULP)\b", r["name"] or "", re.I)][:40]
        if not offices:
            offices = [{"name": "Padang", "lat": -0.95, "lon": 100.35}]

        def fetch():
            host = "customer-air-quality-api.open-meteo.com" if cfg.get("api_key") else "air-quality-api.open-meteo.com"
            q = {"latitude": ",".join(str(round(o["lat"], 3)) for o in offices),
                 "longitude": ",".join(str(round(o["lon"], 3)) for o in offices),
                 "hourly": "us_aqi,pm2_5,pm10", "timezone": "UTC", "past_hours": 1, "forecast_hours": 48}
            if cfg.get("api_key"):
                q["apikey"] = cfg["api_key"]
            data = json.loads(http_get(f"https://{host}/v1/air-quality?" + urllib.parse.urlencode(q), timeout=40,
                                       max_bytes=20_000_000))
            data = data if isinstance(data, list) else [data]
            return [{"name": o["name"], "lat": o["lat"], "lon": o["lon"], "hourly": d.get("hourly") or {}}
                    for o, d in zip(offices, data)]

        rec = self._om_series("aqi", fetch, len(offices))
        at = time.time() + 3600 * offset_h
        pts = []
        for d in rec["data"]:
            h = d["hourly"]
            try:
                k = self._hour_index(h.get("time") or [], at)
            except FetchError:
                continue
            g = lambda name: (h.get(name) or [None] * (k + 1))[k]
            pts.append({"name": d["name"], "lat": d["lat"], "lon": d["lon"], "us_aqi": g("us_aqi"),
                        "pm2_5": g("pm2_5"), "pm10": g("pm10"), "time": h["time"][k]})
        if not pts:
            raise FetchError("Data kualitas udara tersimpan sudah tidak mencakup jam ini")
        return {"source": "Open-Meteo Air Quality (model CAMS global)", "scale": "US AQI",
                "note": "Model global resolusi ±40 km; bukan ISPU KLHK dan bukan sensor di gardu.",
                "fetched_at": dt.datetime.fromtimestamp(rec["at"], dt.timezone.utc).isoformat(),
                "offset_h": offset_h, "points": pts}

    def om_usage(self) -> dict:
        return {**USAGE.get("openmeteo", {}), "ttl_min": OM_TTL // 60,
                "note": "Open-Meteo menghitung tiap lokasi sebagai 1 panggilan (gratis 10.000/hari, non-komersial)."}

    def bmkg_usage(self) -> dict:
        return {**USAGE.get("bmkg", {}), "note": "Batas BMKG 60 permintaan/menit per IP."}

    def radar_meta(self) -> dict:
        if not appsettings.get(self.db)["rainviewer"].get("enabled"):
            raise PermissionError("Lapisan radar RainViewer belum diaktifkan Admin.")
        def fetch():
            count("rainviewer")
            return json.loads(http_get("https://api.rainviewer.com/public/weather-maps.json", timeout=15))
        return self._cached("radar", 300, fetch)

    # ---------------------------------------------------- simulation
    def start_simulation(self) -> cap.Warning:
        now = dt.datetime.now(dt.timezone.utc)
        self.simulation = cap.Warning(
            id="SIMULASI-CAP-001", title="SIMULASI: hujan lebat disertai petir",
            description="Contoh area peringatan di sekitar Kota Padang untuk latihan operator. BUKAN data BMKG.",
            instruction="Latihan: verifikasi kondisi lapangan dan kesiapan petugas sesuai SOP setempat.",
            severity="Severe", certainty="Likely", urgency="Expected", status="Actual", msg_type="Alert",
            sent=(now - dt.timedelta(minutes=1)).isoformat(), effective=(now - dt.timedelta(minutes=1)).isoformat(),
            expires=(now + dt.timedelta(hours=2)).isoformat(), areas_text=["Area simulasi Kota Padang"],
            areas=[{"type": "Polygon", "coordinates": [[[100.33, -0.86], [100.42, -0.86], [100.45, -0.97],
                                                       [100.37, -0.99], [100.33, -0.86]]]}])
        self.simulation_exposure = compute_exposure(self.ctx.index, self.simulation,
                                                    float(appsettings.get(self.db).get("corridor_m") or 0))
        return self.simulation

    def stop_simulation(self):
        self.simulation = None
        self.simulation_exposure = []

    # -------------------------------------------------- notifications
    def process_notifications(self, sender=None) -> int:
        cfg = appsettings.get(self.db)["wa"]
        rows = self.db.all("SELECT * FROM notifications WHERE status='pending' AND next_attempt_at <= now() "
                           "ORDER BY id LIMIT 20")
        if not rows:
            return 0
        if not cfg.get("enabled") or not cfg.get("url"):
            return 0
        sent = 0
        for r in rows:
            try:
                status, text = (sender or send_wa)(cfg, r["target"], r["message"])
                if 200 <= status < 300:
                    self.db.run("UPDATE notifications SET status='sent', sent_at=now(), attempts=attempts+1, error=NULL "
                                "WHERE id=%s", (r["id"],))
                    sent += 1
                    continue
                err = f"HTTP {status}: {text[:300]}"
            except FetchError as exc:
                err = str(exc)
            attempts = r["attempts"] + 1
            final = attempts >= 6
            self.db.run("UPDATE notifications SET attempts=%s, error=%s, status=%s, "
                        "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
                        (attempts, err[:500], "failed" if final else "pending", float(min(3600, 30 * 2 ** attempts)),
                         r["id"]))
        set_health(self.db, "wa_gateway", "ok" if sent else "error",
                   None if sent else "Pengiriman ke WA gateway gagal; lihat daftar notifikasi")
        return sent


def km_between(a: tuple, b: tuple) -> float:
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def quake_item(g: dict) -> dict:
    lat, lon = (float(v) for v in g["Coordinates"].split(",")[:2])
    return {"datetime": g.get("DateTime"), "tanggal": g.get("Tanggal"), "jam": g.get("Jam"),
            "magnitude": g.get("Magnitude"), "kedalaman": g.get("Kedalaman"), "wilayah": g.get("Wilayah"),
            "dirasakan": g.get("Dirasakan"), "potensi": g.get("Potensi"), "lat": lat, "lon": lon,
            "km_padang": round(km_between(PADANG, (lat, lon)))}


def render_body(template: str, target: str, message: str) -> bytes:
    enc = lambda s: json.dumps(s, ensure_ascii=False)[1:-1]
    return template.replace("{{target}}", enc(target)).replace("{{message}}", enc(message)).encode("utf-8")


def send_wa(cfg: dict, target: str, message: str) -> tuple[int, str]:
    body = render_body(cfg.get("body_template") or appsettings.DEFAULTS["wa"]["body_template"], target, message)
    headers = {"Content-Type": "application/json"}
    headers.update({k: v for k, v in (cfg.get("headers") or {}).items() if k and v})
    return http_post(cfg["url"], body, headers)


def enqueue(db, cfg: dict, dedupe: str, message: str) -> int:
    n = 0
    for target in cfg["wa"].get("targets") or []:
        target = str(target).strip()
        if not target:
            continue
        n += max(db.run("INSERT INTO notifications(channel, target, message, dedupe_key) VALUES ('wa', %s, %s, %s) "
                        "ON CONFLICT (dedupe_key) DO NOTHING", (target, message, f"{dedupe}:{target}")), 0)
    return n
