"""Unit tests adapter Tomorrow.io GRIDCAST (tanpa jaringan dan tanpa database)."""
import datetime as dt
import tempfile
import types
from pathlib import Path

from dcc import appsettings, tomorrow as tio

NOW = dt.datetime(2026, 10, 29, 2, 49, tzinfo=dt.timezone.utc).timestamp()
HOURLY = [
    {"time": "2026-10-29T02:00:00Z", "values": {"windSpeed": 1.3, "windDirection": 310, "windGust": 3, "rainIntensity": 0,
                                                "precipitationProbability": 0, "weatherCode": 1001, "temperature": 25.6}},
    {"time": "2026-10-29T03:00:00Z", "values": {"rainIntensity": 0.2, "precipitationProbability": 55, "weatherCode": 2100}},
    {"time": "2026-10-29T04:00:00Z", "values": {"rainIntensity": 0.6, "precipitationProbability": 50, "weatherCode": 4200}},
    {"time": "2026-10-29T06:00:00Z", "values": {"rainIntensity": 12, "precipitationProbability": 80, "weatherCode": 4201}},
    {"time": "2026-10-29T01:00:00Z", "values": {"rainIntensity": 30, "precipitationProbability": 99, "weatherCode": 8000}},
]
MINUTELY = [{"time": f"2026-10-29T02:{m:02d}:00Z", "values": {"rainIntensity": 1.2 if m >= 58 else 0}} for m in range(45, 60)]
DAILY = [{"time": "2026-10-28T23:00:00Z", "values": {"precipitationProbabilityMax": 80, "rainAccumulationSum": 14.2,
                                                     "windGustMax": 9, "weatherCodeMax": 4201}}]


def test_tiles_and_plan_fit_free_quota():
    tiles = tio.tiles_for()
    assert len(tiles) == 10 and (7, 99, 64) in tiles and (5, 24, 15) in tiles
    assert tio.effective_interval(8, 120) == 150
    assert tio.plan_per_hour(8, 150) == 19.2 and 19.2 * 24 <= tio.DAY_LIMIT - tio.DAY_SPARE
    # Interval terlalu rapat dipanjangkan otomatis sehingga kuota harian aman.
    for pts, iv in ((8, 30), (15, 30), (15, 60)):
        eff = tio.effective_interval(pts, iv)
        assert tio.plan_per_hour(pts, eff) * 24 <= tio.DAY_LIMIT
        assert tio.plan_per_hour(pts, eff) <= tio.HOUR_LIMIT - tio.RESERVE
    assert tio.frame_times(NOW) == ["2026-10-29T02:00:00Z", "2026-10-29T03:00:00Z", "2026-10-29T04:00:00Z"]


def test_summary_from_cache_slides_with_time():
    s = tio.summarize(HOURLY, now=NOW, minutely=MINUTELY, daily=DAILY)
    assert s["time"] == "2026-10-29T02:00:00Z" and s["ws"] == 4.7 and s["wg"] == 10.8
    assert s["eta_rain"] == "2026-10-29T04:00:00Z" and s["eta_heavy"] == "2026-10-29T06:00:00Z"
    assert s["nowcast"]["rain_start_min"] == 9                 # hujan per menit mulai 02:58
    assert s["daily"][0]["prob"] == 80
    later = tio.summarize(HOURLY, now=NOW + 3 * 3600, minutely=MINUTELY)   # data sama dipakai 3 jam kemudian
    assert later["time"] == "2026-10-29T04:00:00Z" and later["nowcast"] is None


def test_budget_clock_hour_and_restart():
    d = Path(tempfile.mkdtemp())
    b = tio.Budget(hour_limit=3, day_limit=5, path=d / "u.json")
    for i in range(3):
        assert b.allow(1, NOW + i)
        b.record(NOW + i)
    assert not b.allow(1, NOW + 10) and b.allow(1, NOW + 3700)
    b2 = tio.Budget(hour_limit=5, day_limit=50, path=d / "u.json")
    assert b2.allow(1, NOW + 20) and not b2.allow(3, NOW + 20)     # 3 panggilan sebelum restart tetap dihitung
    b2.exhaust_hour(NOW + 20)
    assert not tio.Budget(hour_limit=5, day_limit=50, path=d / "u.json").allow(1, NOW + 30)


def _adapter(cfg, data_dir=None, hour_limit=tio.HOUR_LIMIT):
    ctx = types.SimpleNamespace(db=types.SimpleNamespace(), index=None)
    if data_dir is not None:
        ctx.settings = types.SimpleNamespace(data_dir=data_dir)
    t = tio.Tomorrow(ctx)
    t.budget = tio.Budget(hour_limit=hour_limit, day_limit=500)
    t.cfg = lambda: cfg
    calls = []
    start = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    hourly = [{"time": (start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "values": HOURLY[0]["values"]}
              for i in range(120)]

    def fake_get(path, params, key, binary=False, kind="data"):
        if not t.budget.allow(1):
            raise tio.TomorrowError("Kuota aman Tomorrow.io tercapai; pengambilan ditunda.")
        t.budget.record()
        calls.append(path)
        return b"\x89PNG\r\n\x1a\nx" if binary else {"timelines": {"hourly": hourly, "daily": [], "minutely": []}}

    t._get = fake_get
    t._health = lambda: None
    return t, calls


CFG = {**appsettings.DEFAULTS["tomorrow"], "enabled": True, "api_key": "k", "max_points": 3}


def test_scheduler_priority_detail_then_sites_then_coarse():
    t, calls = _adapter(CFG, hour_limit=6 * 3 + 3 + tio.RESERVE)   # cukup untuk 3 frame detail + 3 titik
    t.refresh()
    tiles = [c for c in calls if "/map/tile/" in c]
    assert len(tiles) == 18 and all("/map/tile/7/" in c for c in tiles)
    assert sum("/weather/forecast" in c for c in calls) == 3
    v = t.view()
    assert len(v["points"]) == 3 and [f["offset_h"] for f in v["tiles"]["frames"]] == [0, 1, 2]
    assert v["tiles"]["frames"][0]["layers"][1]["available"] == 6
    assert v["tiles"]["frames"][0]["layers"][0]["available"] == 0          # tile Sumatra menunggu jam berikut


def test_full_fill_then_no_calls_and_cache_survives_restart():
    d = Path(tempfile.mkdtemp())
    t, calls = _adapter(CFG, d, hour_limit=200)
    t.refresh()
    assert len(calls) == tio.MAX_CALLS_PER_CYCLE            # satu siklus dibatasi, sisanya siklus berikut
    t.refresh()
    assert len(calls) == 3 + 10 * 3 + 6                     # titik + 3 frame + nowcast
    t.refresh()
    assert len(calls) == 39                                 # semua masih berlaku: tidak ada panggilan baru
    t2, calls2 = _adapter(CFG, d, hour_limit=200)           # server direstart
    t2.refresh()
    assert calls2 == [] and len(t2.view()["points"]) == 3
    assert t2.tile(7, 99, 64).startswith(b"\x89PNG") and t2.tile(7, 1, 1) == tio.EMPTY_PNG
    f1 = tio.frame_times()[1]
    assert t2.tile(7, 99, 64, f1).startswith(b"\x89PNG")


def test_nowcast_cut_by_quota_resumes_without_refetching():
    t, calls = _adapter(CFG, hour_limit=3 + 10 * 3 + 3 + tio.RESERVE)     # kuota habis setelah 3 dari 6 tile nowcast
    t.refresh()
    t.refresh()
    nowcast = lambda: sum(c.endswith("/now.png") for c in calls)
    assert nowcast() == 3
    t.budget.hour_limit = 200
    t.refresh()
    assert len({c for c in calls if c.endswith("/now.png")}) == 6
    assert nowcast() == 6
    t.refresh()
    assert nowcast() == 6                                                 # sudah segar untuk jam ini


def test_view_offset_uses_same_cache():
    t, calls = _adapter(CFG, hour_limit=200)
    t.refresh()
    n = len(calls)
    v = t.view(offset_h=3)
    assert len(calls) == n and v["offset_h"] == 3 and v["points"][0]["nowcast"] is None


def test_disabled_adapter_is_not_called():
    t, calls = _adapter({**CFG, "enabled": False})
    assert t.refresh() is None and not calls
    try:
        t.view()
        raise AssertionError
    except PermissionError:
        pass


def test_api_key_masked():
    cfg = appsettings._merge(appsettings.DEFAULTS, {"tomorrow": {"api_key": "rahasia", "enabled": True}})
    assert appsettings.masked(cfg)["tomorrow"]["api_key"] == "********"
    assert "api_key" not in appsettings.public_view(cfg)["tomorrow"]
