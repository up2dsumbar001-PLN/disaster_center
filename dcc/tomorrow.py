"""Adapter Tomorrow.io: prakiraan per titik UP3 dan tile intensitas hujan.

Paket gratis dibatasi 25 panggilan/jam, 500/hari, 3/detik; setiap tile peta
dihitung satu panggilan. Karena itu server yang mengambil data (bukan
browser), dengan jadwal tetap dan cache bersama untuk semua pengguna:

* prakiraan 1 jam-an per lokasi (1 panggilan/lokasi), dan
* tile ``precipitationIntensity`` "now": zoom 5 untuk seluruh Sumatra (4 tile,
  dipakai saat peta di-zoom out) dan zoom 7 untuk Sumatera Barat (6 tile).

Dengan 8 lokasi dan interval 60 menit pemakaian 18 panggilan/jam (432/hari).
API key tidak pernah dikirim ke browser.

Data Tomorrow.io adalah model/nowcast, BUKAN peringatan resmi. Peringatan
resmi tetap dari BMKG.
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
UA = "DCC-DisasterCenter/1.0 (+intranet PLN UP2D Sumbar)"
SUMBAR_BBOX = (98.5, -3.6, 101.95, 0.95)
SUMATRA_BBOX = (95.0, -6.0, 106.0, 6.0)
TILE_ZOOM = 7
# (zoom, bbox): lapisan kasar Sumatra + lapisan detail Sumbar.
TILE_SETS = ((5, SUMATRA_BBOX), (TILE_ZOOM, SUMBAR_BBOX))
TILE_FIELD = "precipitationIntensity"
HOUR_LIMIT = 22   # batas aman di bawah 25/jam paket gratis
DAY_LIMIT = 470   # batas aman di bawah 500/hari paket gratis
OUTLOOK_HOURS = 6

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

# PNG transparan 1x1 untuk tile di luar Sumbar atau belum tersedia.
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


def _ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _num(v, k=1.0, nd=1):
    return None if v is None else round(float(v) * k, nd)


def summarize(hourly: list[dict], now: float | None = None) -> dict:
    """Ringkas timeline hourly Tomorrow.io: kondisi jam ini + prospek 6 jam."""
    now = time.time() if now is None else now
    rows = [(_ts(h["time"]), h.get("values") or {}) for h in hourly if h.get("time")]
    rows.sort(key=lambda r: r[0])
    if not rows:
        raise TomorrowError("Timeline kosong")
    i = max([k for k, (t, _) in enumerate(rows) if t <= now] or [0])
    t0, v = rows[i]
    window = rows[i:i + OUTLOOK_HOURS + 1]
    code = int(v.get("weatherCode") or 0)
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")
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
        "outlook": outlook,
    }


def calls_per_hour(points: int, interval_min: int) -> float:
    return (points + len(tiles_for())) * 60.0 / max(1, interval_min)


class Budget:
    """Penghitung panggilan per jam jam-dinding UTC dan per hari UTC.

    Tomorrow.io mereset kuota jam di awal setiap jam dan kuota hari pukul
    00:00 UTC, jadi penghitung mengikuti jam-dinding (bukan jendela geser).
    Nilainya disimpan ke disk supaya restart server tidak membuat penghitung
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

    def allow(self, n: int = 1, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            self._roll(now)
            return self.hour + n <= self.hour_limit and self.day + n <= self.day_limit

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
            self.dir.mkdir(parents=True, exist_ok=True)
        self.budget = Budget(path=self.dir / "usage.json" if self.dir else None)
        self.lock = threading.Lock()
        self.points: list[dict] | None = None
        self.points_at = 0.0
        self.tiles: dict = {}
        self.tiles_at = 0.0
        self.tiles_time: str | None = None
        self.last_error: str | None = None
        self.paused_until = 0.0
        self._load_cache()

    # ------------------------------------------------- disk cache
    def _load_cache(self):
        """Muat cache terakhir agar restart server tidak menghabiskan kuota untuk mengambil ulang."""
        if not self.dir:
            return
        try:
            f = self.dir / "points.json"
            if f.exists():
                d = json.loads(f.read_text(encoding="utf-8"))
                self.points, self.points_at = d.get("points"), float(d.get("at") or 0)
            f = self.dir / "tiles.json"
            if f.exists():
                d = json.loads(f.read_text(encoding="utf-8"))
                self.tiles_time, self.tiles_at = d.get("time"), float(d.get("at") or 0)
                for z, x, y in tiles_for():
                    t = self.dir / f"tile_{z}_{x}_{y}.png"
                    if t.exists():
                        self.tiles[(z, x, y)] = t.read_bytes()
            f = self.dir / "pause.json"
            if f.exists():
                self.paused_until = float(json.loads(f.read_text(encoding="utf-8")).get("until") or 0)
        except (ValueError, OSError) as exc:
            log.warning("cache Tomorrow.io tidak terbaca: %s", exc)

    def _save(self, name: str, data):
        if not self.dir:
            return
        try:
            (self.dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("gagal menyimpan cache Tomorrow.io: %s", exc)

    def cfg(self) -> dict:
        return appsettings.get(self.db)["tomorrow"]

    # ---------------------------------------------------------- http
    def _get(self, path: str, params: dict, key: str, binary=False):
        if not self.budget.allow(1):
            raise TomorrowError("Kuota aman Tomorrow.io tercapai; pengambilan ditunda.")
        url = f"{API}{path}?" + urllib.parse.urlencode({**params, "apikey": key})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate",
                                                   "Accept": "image/png" if binary else "application/json"})
        self.budget.record()
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

    # ----------------------------------------------------- refresh
    def refresh(self, force: bool = False) -> dict | None:
        """Dipanggil collector tiap siklus; hanya mengambil data bila sudah kedaluwarsa."""
        cfg = self.cfg()
        if not cfg.get("enabled") or not cfg.get("api_key"):
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            now = time.time()
            if now < self.paused_until and not force:
                return None
            ttl = max(30, int(cfg.get("interval_min") or 60)) * 60
            did = {}
            if force or self.points is None or now - self.points_at >= ttl:
                did["points"] = self._refresh_points(cfg)
            incomplete = len(self.tiles) < len(tiles_for())  # lengkapi tile yang tertunda karena kuota
            if cfg.get("rain_tiles", True) and (force or incomplete or now - self.tiles_at >= ttl):
                did["tiles"] = self._refresh_tiles(cfg)
            if did:
                self._health()
            return did
        finally:
            self.lock.release()

    def _refresh_points(self, cfg) -> int:
        pts, errs = [], []
        for s in self.sites(cfg):
            try:
                data = self._get("/weather/forecast", {"location": f"{s['lat']},{s['lon']}", "timesteps": "1h",
                                                       "units": "metric"}, cfg["api_key"])
                pts.append({**s, **summarize(data["timelines"]["hourly"])})
            except (TomorrowError, KeyError, ValueError, TypeError) as exc:
                errs.append(f"{s['name']}: {exc}")
                if isinstance(exc, TomorrowError) and ("429" in str(exc) or "Kuota" in str(exc) or "401" in str(exc)):
                    break
        if pts:
            self.points, self.points_at = pts, time.time()
            self._save("points.json", {"at": self.points_at, "points": pts})
        self.last_error = "; ".join(errs)[:800] or None
        return len(pts)

    def _refresh_tiles(self, cfg) -> int:
        """Tile detail Sumbar diambil lebih dulu. Tile yang gagal tetap memakai versi sebelumnya."""
        got = {}
        stamp = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")
        order = sorted(tiles_for(), key=lambda t: (t[0] != TILE_ZOOM, t))
        for z, x, y in order:
            try:
                png = self._get(f"/map/tile/{z}/{x}/{y}/{TILE_FIELD}/now.png", {}, cfg["api_key"], binary=True)
                if png[:8] == b"\x89PNG\r\n\x1a\n":
                    got[(z, x, y)] = png
            except TomorrowError as exc:
                self.last_error = f"tile {z}/{x}/{y}: {exc}"
                break
        if got:
            self.tiles = {**self.tiles, **got}
            self.tiles_at, self.tiles_time = time.time(), stamp
            for (z, x, y), png in got.items():
                if self.dir:
                    try:
                        (self.dir / f"tile_{z}_{x}_{y}.png").write_bytes(png)
                    except OSError:
                        pass
            self._save("tiles.json", {"at": self.tiles_at, "time": stamp})
        return len(got)

    def _health(self):
        from .collector import set_health  # impor lambat: hindari impor melingkar
        n_tiles = len(tiles_for())
        if self.points and (not self.last_error):
            state = "ok"
        elif self.points or self.tiles:
            state = "partial"
        else:
            state = "error"
        set_health(self.db, "tomorrow", state, self.last_error,
                   {"points": len(self.points or []), "tiles": f"{len(self.tiles)}/{n_tiles}", **self.budget.usage()})

    # ------------------------------------------------------- views
    def view(self) -> dict:
        cfg = self.cfg()
        if not cfg.get("enabled"):
            raise PermissionError("Adapter Tomorrow.io belum diaktifkan Admin.")
        if not cfg.get("api_key"):
            raise PermissionError("API key Tomorrow.io belum diisi Admin.")
        if self.points is None:
            self.refresh()
        if self.points is None and not self.tiles:
            raise TomorrowError(self.last_error or "Data Tomorrow.io belum tersedia, coba lagi sebentar.")
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat() if t else None
        return {
            "source": "Tomorrow.io (model & nowcast global)",
            "note": "Prakiraan model, bukan peringatan resmi. Peringatan resmi tetap dari BMKG.",
            "direction_note": "Arah panah = arah angin bertiup (menuju). Nilai derajat = arah datang angin.",
            "points_at": iso(self.points_at), "points": self.points or [],
            "tiles": {"time": self.tiles_time, "fetched_at": iso(self.tiles_at), "zoom": TILE_ZOOM,
                      "count": len(self.tiles), "field": TILE_FIELD,
                      "layers": [{**L, "available": sum(1 for t in self.tiles if t[0] == L["zoom"])} for L in tile_layers()],
                      "detail_bounds": [[SUMBAR_BBOX[1], SUMBAR_BBOX[0]], [SUMBAR_BBOX[3], SUMBAR_BBOX[2]]]},
            "usage": self.budget.usage(), "error": self.last_error,
        }

    def tile(self, z: int, x: int, y: int) -> bytes:
        if not self.cfg().get("enabled"):
            raise PermissionError("Adapter Tomorrow.io belum diaktifkan Admin.")
        return self.tiles.get((z, x, y)) or EMPTY_PNG

    def test(self) -> dict:
        cfg = self.cfg()
        if not cfg.get("api_key"):
            raise TomorrowError("Isi API key Tomorrow.io terlebih dahulu.")
        s = self.sites(cfg)[0]
        data = self._get("/weather/forecast", {"location": f"{s['lat']},{s['lon']}", "timesteps": "1h",
                                               "units": "metric"}, cfg["api_key"])
        sm = summarize(data["timelines"]["hourly"])
        return {"site": s["name"], "now": {k: sm[k] for k in ("time", "desc", "temp", "ws", "wd", "rain", "prob")},
                "eta_rain": sm["eta_rain"], "usage": self.budget.usage(),
                "calls_per_hour": calls_per_hour(len(self.sites(cfg)), int(cfg.get("interval_min") or 60))}
