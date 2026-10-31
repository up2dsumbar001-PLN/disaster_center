"""Adapter Tomorrow.io untuk GRIDCAST: prakiraan per kantor UP3 dan tile hujan.

Prinsip hemat kuota (paket gratis 25 panggilan/jam, 500/hari, 3/detik):

* **Satu panggilan prakiraan = tiga timeline.** ``/weather/forecast`` tanpa
  ``timesteps`` mengembalikan per-menit 60 menit, per-jam 120 jam dan harian
  6 hari. Semuanya disimpan; kondisi "saat ini", prospek 6 jam dan 5 hari
  dihitung ulang dari cache setiap kali diminta, sehingga data tetap terpakai
  berjam-jam tanpa panggilan baru.
* **Tile disimpan per jam berlaku.** Tile untuk jam H, H+1, H+2 diambil sekali
  (prakiraan) dan dipakai sampai jamnya lewat. Tiap jam cukup menambah satu
  frame baru (H+2). Tile "now" (nowcast) untuk jam berjalan disegarkan sekali
  per jam bila kuota masih longgar.
* **Penjadwal berprioritas** menghabiskan kuota per jam secara merata:
  tile detail Sumbar > titik UP3 > tile Sumatra > penyegaran nowcast.
* Semua data dan penghitung kuota disimpan di ``data/tomorrow`` sehingga
  restart server tidak memakai kuota lagi.

Data Tomorrow.io adalah model/nowcast, BUKAN peringatan resmi. Peringatan
resmi tetap dari BMKG. API key tidak pernah dikirim ke browser.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from . import appsettings

log = logging.getLogger("dcc.tomorrow")

API = "https://api.tomorrow.io/v4"
UA = "DCC-DisasterCenter/1.0 (+intranet PLN UP2D Sumbar)"  # identifier teknis, sengaja tidak diubah
SUMBAR_BBOX = (98.5, -3.6, 101.95, 0.95)
SUMATRA_BBOX = (95.0, -6.0, 106.0, 6.0)
TILE_ZOOM = 7
# (zoom, bbox): lapisan kasar Sumatra + lapisan detail Sumbar.
TILE_SETS = ((5, SUMATRA_BBOX), (TILE_ZOOM, SUMBAR_BBOX))
TILE_FIELD = "precipitationIntensity"
HOUR_LIMIT = 22      # batas aman di bawah 25/jam paket gratis
DAY_LIMIT = 470      # batas aman di bawah 500/hari paket gratis
DAY_SPARE = 9        # panggilan/hari yang tidak direncanakan, ruang untuk uji koneksi Admin
RESERVE = 2          # sisa per jam untuk uji koneksi Admin
FRAMES_AHEAD = 2     # frame jam berjalan + 2 jam ke depan
OUTLOOK_HOURS = 6
MAX_CALLS_PER_CYCLE = 25

FALLBACK_SITES = [
    ("Padang", -0.947, 100.417), ("Bukittinggi", -0.305, 100.369), ("Payakumbuh", -0.220, 100.633),
    ("Solok", -0.790, 100.655), ("Pariaman", -0.626, 100.120), ("Sawahlunto", -0.680, 100.780),
    ("Painan", -1.350, 100.575), ("Lubuk Sikaping", 0.140, 100.165), ("Pulau Punjung", -1.000, 101.450),
    ("Tuapejat", -2.030, 99.590),
]

WEATHER_CODES = {
    0: "Tidak diketahui", 1000: "Cerah", 1100: "Cerah berawan", 1101: "Berawan sebagian", 1102: "Berawan",
    1001: "Berawan tebal", 2000: "Kabut", 2100: "Kabut tipis", 4000: "Gerimis", 4001: "Hujan",
    4200: "Hujan ringan", 4201: "Hujan lebat", 8000: "Badai petir",
}
RAIN_CODES = {4000, 4001, 4200, 4201, 8000}
HEAVY_CODES = {4201, 8000}
HOURLY_KEEP = ("temperature", "humidity", "windSpeed", "windGust", "windDirection", "rainIntensity",
               "precipitationProbability", "weatherCode", "cloudCover", "visibility", "rainAccumulation")
DAILY_KEEP = ("precipitationProbabilityMax", "rainAccumulationSum", "rainIntensityMax", "windGustMax",
              "windSpeedMax", "temperatureMax", "temperatureMin", "weatherCodeMax")
MINUTELY_KEEP = ("rainIntensity", "precipitationProbability")

# PNG transparan 1x1 untuk tile di luar cakupan atau belum tersedia.
EMPTY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e5274de40000000049454e44ae426082")


class TomorrowError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def lon2x(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * (1 << z))


def lat2y(lat: float, z: int) -> int:
    r = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (1 << z))


def tiles_in(bbox, z: int) -> list[tuple[int, int, int]]:
    x0, x1 = lon2x(bbox[0], z), lon2x(bbox[2], z)
    y0, y1 = lat2y(bbox[3], z), lat2y(bbox[1], z)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def tile_bounds(z: int, x0: int, x1: int, y0: int, y1: int) -> list:
    n = 1 << z
    lat = lambda y: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return [[lat(y1 + 1), x0 / n * 360 - 180], [lat(y0), (x1 + 1) / n * 360 - 180]]


def tile_layers() -> list[dict]:
    out = []
    for z, bb in TILE_SETS:
        t = tiles_in(bb, z)
        xs, ys = [a[1] for a in t], [a[2] for a in t]
        out.append({"zoom": z, "bounds": tile_bounds(z, min(xs), max(xs), min(ys), max(ys)), "count": len(t)})
    return out


def tiles_for() -> list[tuple[int, int, int]]:
    return [t for z, bb in TILE_SETS for t in tiles_in(bb, z)]


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frame_times(now: float | None = None) -> list[str]:
    """Jam berlaku tile: jam berjalan (UTC) dan FRAMES_AHEAD jam ke depan."""
    now = time.time() if now is None else now
    h = int(now // 3600) * 3600
    return [iso(h + 3600 * i) for i in range(FRAMES_AHEAD + 1)]


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _num(v, k=1.0, nd=1):
    return None if v is None else round(float(v) * k, nd)


def _trim(rows: list[dict], keep: tuple) -> list[dict]:
    return [{"time": r["time"], "values": {k: (r.get("values") or {}).get(k) for k in keep}}
            for r in rows or [] if r.get("time")]


def plan_per_hour(points: int, interval_min: int, tiles: bool = True) -> float:
    """Perkiraan panggilan/jam pada kondisi tunak (setelah cache terisi)."""
    detail = sum(1 for t in tiles_for() if t[0] == TILE_ZOOM)
    tile_calls = (len(tiles_for()) + detail) if tiles else 0
    return tile_calls + points * 60.0 / max(1, interval_min)


calls_per_hour = plan_per_hour  # nama lama, dipakai validasi pengaturan


def effective_interval(points: int, interval_min: int, tiles: bool = True) -> int:
    """Interval titik dipanjangkan otomatis bila pengaturan Admin melebihi kuota harian."""
    room = (DAY_LIMIT - DAY_SPARE) / 24.0 - plan_per_hour(0, 60, tiles)
    need = math.ceil(points * 60.0 / room) if room > 0 else 360
    return max(int(interval_min or 120), need, 30)


def summarize(hourly: list[dict], now: float | None = None, minutely: list[dict] | None = None,
              daily: list[dict] | None = None) -> dict:
    """Ringkas timeline tersimpan untuk waktu ``now``: kondisi saat itu, prospek 6 jam, nowcast, 5 hari."""
    now = time.time() if now is None else now
    rows = sorted(((_ts(h["time"]), h.get("values") or {}) for h in hourly if h.get("time")), key=lambda r: r[0])
    if not rows:
        raise TomorrowError("Timeline kosong")
    if now > rows[-1][0] + 3600:
        raise TomorrowError("Data prakiraan tersimpan sudah habis masa berlakunya")
    i = max([k for k, (t, _) in enumerate(rows) if t <= now] or [0])
    t0, v = rows[i]
    window = rows[i:i + OUTLOOK_HOURS + 1]
    code = int(v.get("weatherCode") or 0)
    eta_rain = eta_heavy = None
    outlook = []
    for t, w in window:
        rain, prob, c = w.get("rainIntensity") or 0, w.get("precipitationProbability") or 0, int(w.get("weatherCode") or 0)
        if eta_rain is None and ((rain >= 1.0 and prob >= 40) or (c in RAIN_CODES and prob >= 50)):
            eta_rain = iso(t)
        if eta_heavy is None and (rain >= 10.0 or (c in HEAVY_CODES and prob >= 40)):
            eta_heavy = iso(t)
        outlook.append({"time": iso(t), "rain": _num(rain), "prob": prob, "code": c,
                        "desc": WEATHER_CODES.get(c, str(c)), "wg": _num(w.get("windGust"), 3.6)})
    # Nowcast per menit: hanya dipakai selama timeline-nya masih mencakup waktu ini.
    nowcast = None
    mrows = sorted(((_ts(m["time"]), m.get("values") or {}) for m in minutely or [] if m.get("time")), key=lambda r: r[0])
    if mrows and mrows[0][0] - 120 <= now <= mrows[-1][0]:
        ahead = [(t, m) for t, m in mrows if t >= now - 60]
        start = next((t for t, m in ahead if (m.get("rainIntensity") or 0) >= 0.5), None)
        nowcast = {"until": iso(mrows[-1][0]), "rain_now": _num(ahead[0][1].get("rainIntensity")) if ahead else None,
                   "rain_start_min": None if start is None else max(0, round((start - now) / 60)),
                   "max_rain": _num(max((m.get("rainIntensity") or 0) for _, m in ahead)) if ahead else None}
    days = []
    for d in daily or []:
        w = d.get("values") or {}
        if not d.get("time") or _ts(d["time"]) + 86400 < now:
            continue
        c = int(w.get("weatherCodeMax") or 0)
        days.append({"time": d["time"], "prob": w.get("precipitationProbabilityMax"), "rain": _num(w.get("rainAccumulationSum")),
                     "wg": _num(w.get("windGustMax"), 3.6), "tmax": _num(w.get("temperatureMax")),
                     "tmin": _num(w.get("temperatureMin")), "code": c, "desc": WEATHER_CODES.get(c, str(c))})
    return {
        "time": iso(t0), "temp": _num(v.get("temperature")), "hum": v.get("humidity"),
        "ws": _num(v.get("windSpeed"), 3.6), "wg": _num(v.get("windGust"), 3.6), "wd": v.get("windDirection"),
        "rain": _num(v.get("rainIntensity")), "prob": v.get("precipitationProbability"),
        "cloud": _num(v.get("cloudCover"), 1, 0), "vis": _num(v.get("visibility")), "code": code,
        "desc": WEATHER_CODES.get(code, str(code)),
        "eta_rain": eta_rain, "eta_heavy": eta_heavy,
        "max_prob": max((o["prob"] for o in outlook), default=None),
        "max_rain": max((o["rain"] or 0 for o in outlook), default=None),
        "max_gust": max((o["wg"] or 0 for o in outlook), default=None),
        "outlook": outlook, "nowcast": nowcast, "daily": days[:5],
        "valid_until": iso(rows[-1][0]),
    }


class Budget:
    """Penghitung panggilan per jam jam-dinding UTC dan per hari UTC, disimpan ke disk.

    Tomorrow.io mereset kuota jam di awal setiap jam dan kuota hari pukul
    00:00 UTC. Disimpan ke disk agar restart server tidak membuat penghitung
    kembali nol sementara Tomorrow.io masih menghitung panggilan sebelumnya.
    """

    def __init__(self, hour_limit=HOUR_LIMIT, day_limit=DAY_LIMIT, path: Path | None = None):
        self.hour_limit, self.day_limit = hour_limit, day_limit
        self.path = path
        self.hour_key = self.day_key = None
        self.hour = self.day = 0
        self.lock = threading.Lock()
        if path and path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                self.hour_key, self.hour = d.get("hour_key"), int(d.get("hour", 0))
                self.day_key, self.day = d.get("day_key"), int(d.get("day", 0))
            except (ValueError, OSError):
                pass

    @staticmethod
    def keys(now: float):
        t = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        return t.strftime("%Y-%m-%dT%H"), t.strftime("%Y-%m-%d")

    def _roll(self, now):
        hk, dk = self.keys(now)
        if hk != self.hour_key:
            self.hour_key, self.hour = hk, 0
        if dk != self.day_key:
            self.day_key, self.day = dk, 0

    def allow(self, n: int = 1, now: float | None = None, reserve: int = 0) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            self._roll(now)
            return self.hour + n + reserve <= self.hour_limit and self.day + n <= self.day_limit

    def record(self, now: float | None = None):
        now = time.time() if now is None else now
        with self.lock:
            self._roll(now)
            self.hour += 1
            self.day += 1
            self._save()

    def exhaust_hour(self, now: float | None = None):
        """Dipanggil saat HTTP 429: anggap kuota jam ini habis sampai jam berikutnya."""
        now = time.time() if now is None else now
        with self.lock:
            self._roll(now)
            self.hour = max(self.hour, self.hour_limit)
            self._save()

    def _save(self):
        if not self.path:
            return
        try:
            self.path.write_text(json.dumps({"hour_key": self.hour_key, "hour": self.hour,
                                             "day_key": self.day_key, "day": self.day}), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("gagal menyimpan pemakaian Tomorrow.io: %s", exc)

    def usage(self) -> dict:
        with self.lock:
            self._roll(time.time())
            return {"hour": self.hour, "hour_limit": self.hour_limit, "day": self.day, "day_limit": self.day_limit}


# ------------------------------------------------------------------ adapter
class Tomorrow:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        st = getattr(ctx, "settings", None)
        self.dir: Path | None = Path(st.data_dir) / "tomorrow" if st is not None else None
        if self.dir:
            (self.dir / "tiles").mkdir(parents=True, exist_ok=True)
        self.budget = Budget(path=self.dir / "usage.json" if self.dir else None)
        self.lock = threading.Lock()
        self.site_data: dict[str, dict] = {}          # nama -> {lat, lon, fetched_at, hourly, daily, minutely}
        self.tiles: dict[tuple, dict] = {}            # (frame, z, x, y) -> {png, src, at}
        self.last_error: str | None = None
        self.paused_until = 0.0
        self.calls_log: list = []                     # 50 panggilan terakhir untuk analisa
        self._load_cache()

    # ------------------------------------------------- disk cache
    @staticmethod
    def _fname(frame: str, z: int, x: int, y: int) -> str:
        return f"{frame.replace('-', '').replace(':', '')}_{z}_{x}_{y}.png"

    def _load_cache(self):
        if not self.dir:
            return
        try:
            f = self.dir / "sites.json"
            if f.exists():
                self.site_data = json.loads(f.read_text(encoding="utf-8")).get("sites") or {}
            f = self.dir / "tiles_meta.json"
            if f.exists():
                meta = json.loads(f.read_text(encoding="utf-8"))
                for k, m in (meta.get("tiles") or {}).items():
                    frame, z, x, y = k.split("|")
                    p = self.dir / "tiles" / self._fname(frame, int(z), int(x), int(y))
                    if p.exists():
                        self.tiles[(frame, int(z), int(x), int(y))] = {"png": p.read_bytes(), "src": m.get("src"),
                                                                       "at": float(m.get("at") or 0)}
            f = self.dir / "pause.json"
            if f.exists():
                self.paused_until = float(json.loads(f.read_text(encoding="utf-8")).get("until") or 0)
        except (ValueError, OSError) as exc:
            log.warning("cache Tomorrow.io tidak terbaca: %s", exc)
        self._purge()

    def _save(self, name: str, data):
        if not self.dir:
            return
        try:
            tmp = self.dir / (name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.dir / name)
        except OSError as exc:  # pragma: no cover
            log.warning("gagal menyimpan cache Tomorrow.io: %s", exc)

    def _save_tiles_meta(self):
        self._save("tiles_meta.json", {"tiles": {"|".join(map(str, k)): {"src": v["src"], "at": v["at"]}
                                                 for k, v in self.tiles.items()}})

    def _purge(self, now: float | None = None):
        """Buang tile yang jam berlakunya sudah lewat."""
        keep = set(frame_times(now))
        for k in [k for k in self.tiles if k[0] not in keep]:
            self.tiles.pop(k, None)
            if self.dir:
                try:
                    (self.dir / "tiles" / self._fname(*k)).unlink(missing_ok=True)
                except OSError:
                    pass

    def cfg(self) -> dict:
        return appsettings.get(self.db)["tomorrow"]

    # ---------------------------------------------------------- http
    def _get(self, path: str, params: dict, key: str, binary=False, kind="data"):
        if not self.budget.allow(1):
            raise TomorrowError("Kuota aman Tomorrow.io tercapai; pengambilan ditunda.")
        url = f"{API}{path}?" + urllib.parse.urlencode({**params, "apikey": key})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate",
                                                   "Accept": "image/png" if binary else "application/json"})
        self.budget.record()
        self.calls_log = (self.calls_log + [{"at": iso(time.time()), "kind": kind, "path": path}])[-50:]
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(5_000_000)
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    data = gzip.decompress(data)
                elif enc == "deflate":
                    data = zlib.decompress(data)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(600).decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 429:
                now = time.time()
                self.budget.exhaust_hour(now)
                self.paused_until = (now // 3600 + 1) * 3600 + 60  # kuota jam direset di awal jam berikutnya
                self._save("pause.json", {"until": self.paused_until})
                raise TomorrowError("HTTP 429: kuota per jam Tomorrow.io habis, dicoba lagi awal jam berikutnya") from exc
            if exc.code in (401, 403):
                raise TomorrowError(f"HTTP {exc.code}: API key ditolak atau data tidak termasuk paket") from exc
            raise TomorrowError(f"HTTP {exc.code}: {body[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TomorrowError(f"Koneksi gagal: {getattr(exc, 'reason', exc)}") from exc
        time.sleep(0.4)  # batas 3 panggilan/detik
        return data if binary else json.loads(data)

    # ------------------------------------------------------- sites
    def sites(self, cfg: dict) -> list[dict]:
        n = max(1, min(15, int(cfg.get("max_points") or 8)))
        out: list[dict] = []
        idx = getattr(self.ctx, "index", None)
        offices = idx.by_kind.get("kantor", []) if idx is not None else []
        seen = set()
        for r in offices:
            name = (r.get("name") or "").strip()
            if not name.upper().startswith("UP3") or name.upper() in seen or r.get("lat") is None:
                continue
            seen.add(name.upper())
            out.append({"name": name, "lat": round(r["lat"], 4), "lon": round(r["lon"], 4)})
        for name, lat, lon in FALLBACK_SITES:
            if len(out) >= n:
                break
            if all(abs(lat - o["lat"]) + abs(lon - o["lon"]) > 0.15 for o in out):
                out.append({"name": name, "lat": lat, "lon": lon})
        return out[:n]

    def _fetch_site(self, s: dict, key: str) -> dict:
        data = self._get("/weather/forecast", {"location": f"{s['lat']},{s['lon']}", "units": "metric"}, key, kind="titik")
        tl = data.get("timelines") or {}
        if not tl.get("hourly"):
            raise TomorrowError("Respons prakiraan tanpa timeline per jam")
        rec = {"name": s["name"], "lat": s["lat"], "lon": s["lon"], "fetched_at": time.time(),
               "hourly": _trim(tl.get("hourly"), HOURLY_KEEP), "daily": _trim(tl.get("daily"), DAILY_KEEP),
               "minutely": _trim(tl.get("minutely"), MINUTELY_KEEP)}
        self.site_data[s["name"]] = rec
        self._save("sites.json", {"sites": self.site_data})
        return rec

    # ------------------------------------------------- scheduler
    def _tasks(self, cfg: dict, now: float):
        """Daftar pekerjaan berurutan prioritas; dievaluasi ulang tiap siklus collector."""
        frames = frame_times(now)
        tiles_on = cfg.get("rain_tiles", True)
        detail = [t for t in tiles_for() if t[0] == TILE_ZOOM]
        coarse = [t for t in tiles_for() if t[0] != TILE_ZOOM]
        sites = self.sites(cfg)
        ttl = effective_interval(len(sites), int(cfg.get("interval_min") or 120), tiles_on) * 60
        out = []
        if tiles_on:
            out += [("tile", f, t) for f in frames for t in detail if (f, *t) not in self.tiles]
        age = lambda s: now - (self.site_data.get(s["name"]) or {}).get("fetched_at", 0)
        out += [("site", s, None) for s in sorted(sites, key=age, reverse=True) if age(s) >= ttl]
        if tiles_on:
            out += [("tile", f, t) for f in frames for t in coarse if (f, *t) not in self.tiles]
            # Sekali per jam berjalan. Bila terpotong kuota, tile yang sudah segar tidak diambil ulang.
            out += [("now", frames[0], t) for t in detail
                    if (self.tiles.get((frames[0], *t)) or {}).get("src") != "now"]
        return out

    def refresh(self, force: bool = False) -> dict | None:
        """Dipanggil collector tiap siklus (60 detik): jalankan pekerjaan selama kuota jam ini masih ada."""
        cfg = self.cfg()
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            now = time.time()
            if now < self.paused_until and not force:
                return None
            self._purge(now)
            done = {"tile": 0, "site": 0, "now": 0}
            calls = 0
            for kind, a, b in self._tasks(cfg, now):
                if calls >= MAX_CALLS_PER_CYCLE or not self.budget.allow(1, reserve=RESERVE):
                    break
                try:
                    if kind == "site":
                        self._fetch_site(a, cfg["api_key"])
                    else:
                        z, x, y = b
                        when = "now" if kind == "now" else a
                        png = self._get(f"/map/tile/{z}/{x}/{y}/{TILE_FIELD}/{when}.png", {}, cfg["api_key"],
                                        binary=True, kind="tile")
                        if png[:8] != b"\x89PNG\r\n\x1a\n":
                            raise TomorrowError("Tile bukan PNG")
                        self.tiles[(a, z, x, y)] = {"png": png, "src": "now" if kind == "now" else "forecast",
                                                    "at": time.time()}
                        if self.dir:
                            (self.dir / "tiles" / self._fname(a, z, x, y)).write_bytes(png)
                    done[kind] += 1
                    calls += 1
                    self.last_error = None
                except (TomorrowError, OSError, KeyError, ValueError, TypeError) as exc:
                    self.last_error = f"{kind}: {exc}"[:500]
                    break
            if done["tile"] or done["now"]:
                self._save_tiles_meta()
            if calls or self.last_error:
                self._health()
            return done
        finally:
            self.lock.release()

    def _health(self):
        from .collector import set_health  # impor lambat: hindari impor melingkar
        frames = frame_times()
        need = len(tiles_for()) * len(frames)
        have = sum(1 for k in self.tiles if k[0] in frames)
        state = "ok" if self.site_data and not self.last_error else ("partial" if self.site_data or have else "error")
        set_health(self.db, "tomorrow", state, self.last_error,
                   {"points": len(self.site_data), "tiles": f"{have}/{need}", **self.budget.usage()})

    # ------------------------------------------------------- views
    def view(self, offset_h: int = 0) -> dict:
        cfg = self.cfg()
        if not cfg.get("enabled"):
            raise PermissionError("Adapter Tomorrow.io belum diaktifkan Admin.")
        if not cfg.get("api_key"):
            raise PermissionError("API key Tomorrow.io belum diisi Admin.")
        if not self.site_data and not self.tiles:
            self.refresh()
        if not self.site_data and not self.tiles:
            raise TomorrowError(self.last_error or "Data Tomorrow.io belum tersedia, coba lagi sebentar.")
        now = time.time()
        at = now + 3600 * max(0, min(72, int(offset_h or 0)))
        names = {s["name"] for s in self.sites(cfg)}
        points = []
        for name, rec in self.site_data.items():
            if name not in names:
                continue
            try:
                sm = summarize(rec["hourly"], at, rec.get("minutely") if not offset_h else None, rec.get("daily"))
            except TomorrowError:
                continue
            points.append({"name": name, "lat": rec["lat"], "lon": rec["lon"], "fetched_at": iso(rec["fetched_at"]),
                           "age_min": round((now - rec["fetched_at"]) / 60), **sm})
        frames = []
        for i, f in enumerate(frame_times(now)):
            layers = []
            for L in tile_layers():
                ks = [k for k in self.tiles if k[0] == f and k[1] == L["zoom"]]
                layers.append({**L, "available": len(ks)})
            srcs = {self.tiles[k]["src"] for k in self.tiles if k[0] == f}
            frames.append({"time": f, "offset_h": i, "layers": layers,
                           "nowcast": "now" in srcs, "complete": all(l["available"] >= l["count"] for l in layers)})
        sites = self.sites(cfg)
        eff = effective_interval(len(sites), int(cfg.get("interval_min") or 120), cfg.get("rain_tiles", True))
        return {
            "source": "Tomorrow.io (model & nowcast global)",
            "note": "Prakiraan model, bukan peringatan resmi. Peringatan resmi tetap dari BMKG.",
            "direction_note": "Arah panah = arah angin bertiup (menuju). Nilai derajat = arah datang angin.",
            "at": iso(at), "offset_h": offset_h,
            "points_at": max((p["fetched_at"] for p in points), default=None), "points": points,
            "tiles": {"frames": frames, "zoom": TILE_ZOOM, "field": TILE_FIELD,
                      "count": sum(1 for k in self.tiles if k[0] == frames[0]["time"]),
                      "time": frames[0]["time"], "layers": frames[0]["layers"],
                      "detail_bounds": [[SUMBAR_BBOX[1], SUMBAR_BBOX[0]], [SUMBAR_BBOX[3], SUMBAR_BBOX[2]]]},
            "usage": self.budget.usage(), "error": self.last_error,
            "plan": {"per_hour": round(plan_per_hour(len(sites), eff, cfg.get("rain_tiles", True)), 1),
                     "site_interval_min": eff, "sites": len(sites)},
        }

    def tile(self, z: int, x: int, y: int, frame: str | None = None) -> bytes:
        if not self.cfg().get("enabled"):
            raise PermissionError("Adapter Tomorrow.io belum diaktifkan Admin.")
        frames = frame_times()
        f = frame if frame in frames else frames[0]
        hit = self.tiles.get((f, z, x, y))
        if hit is None and f != frames[0]:
            hit = self.tiles.get((frames[0], z, x, y))
        return hit["png"] if hit else EMPTY_PNG

    def usage_report(self) -> dict:
        return {"usage": self.budget.usage(), "recent_calls": self.calls_log[-20:], "error": self.last_error,
                "paused_until": iso(self.paused_until) if self.paused_until > time.time() else None}

    def test(self) -> dict:
        """Uji koneksi: hasilnya ikut disimpan sebagai data titik pertama, jadi tidak terbuang."""
        cfg = self.cfg()
        if not cfg.get("api_key"):
            raise TomorrowError("Isi API key Tomorrow.io terlebih dahulu.")
        s = self.sites(cfg)[0]
        rec = self._fetch_site(s, cfg["api_key"])
        sm = summarize(rec["hourly"], None, rec["minutely"], rec["daily"])
        sites = self.sites(cfg)
        eff = effective_interval(len(sites), int(cfg.get("interval_min") or 120), cfg.get("rain_tiles", True))
        return {"site": s["name"], "now": {k: sm[k] for k in ("time", "desc", "temp", "ws", "wd", "rain", "prob")},
                "eta_rain": sm["eta_rain"], "usage": self.budget.usage(),
                "calls_per_hour": round(plan_per_hour(len(sites), eff, cfg.get("rain_tiles", True)), 1)}
