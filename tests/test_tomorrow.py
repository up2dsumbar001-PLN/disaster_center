"""Unit tests adapter Tomorrow.io (tanpa jaringan dan tanpa database)."""
import datetime as dt
import types

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


def test_tiles_cover_sumbar_at_zoom7():
    tiles = tio.tiles_for()
    assert len(tiles) == 10 and (7, 99, 64) in tiles and (5, 24, 15) in tiles
    assert tio.calls_per_hour(8, 60) == 18 and 18 * 24 <= tio.DAY_LIMIT
    z5, z7 = tio.tile_layers()
    assert z5["bounds"][0][0] < -6 and z5["bounds"][1][1] > 106     # zoom 5 menutup seluruh Sumatra


def test_summary_picks_current_hour_and_eta():
    s = tio.summarize(HOURLY, now=NOW)
    assert s["time"] == "2026-10-29T02:00:00Z"          # jam lalu (01:00) diabaikan
    assert s["ws"] == 4.7 and s["wg"] == 10.8            # m/s -> km/j
    assert s["eta_rain"] == "2026-10-29T04:00:00Z"       # kode hujan ringan, peluang 50%
    assert s["eta_heavy"] == "2026-10-29T06:00:00Z"
    assert s["max_prob"] == 80 and s["desc"] == "Berawan tebal"


def test_budget_blocks_before_free_limit():
    b = tio.Budget(hour_limit=3, day_limit=5)
    for i in range(3):
        assert b.allow(1, NOW + i)
        b.record(NOW + i)
    assert not b.allow(1, NOW + 10)
    assert b.allow(1, NOW + 3700)                         # kuota jam direset di awal jam berikutnya


def test_budget_survives_restart(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tmp_path or tempfile.mkdtemp())
    b = tio.Budget(hour_limit=5, day_limit=50, path=d / "usage.json")
    for _ in range(4):
        b.record()
    b2 = tio.Budget(hour_limit=5, day_limit=50, path=d / "usage.json")   # server direstart
    assert b2.usage()["hour"] == 4 and b2.allow(1) and not b2.allow(2)
    b2.exhaust_hour()
    assert not tio.Budget(hour_limit=5, day_limit=50, path=d / "usage.json").allow(1)


def test_cache_reloaded_after_restart_without_api_calls(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tmp_path or tempfile.mkdtemp())
    cfg = {**appsettings.DEFAULTS["tomorrow"], "enabled": True, "api_key": "k", "max_points": 2}
    t, calls = _adapter(cfg, d)
    t.refresh()
    assert len(calls) == 2 + 10
    t2, calls2 = _adapter(cfg, d)                         # server direstart
    t2.refresh()
    assert calls2 == [] and len(t2.view()["points"]) == 2 and t2.tile(7, 99, 64).startswith(b"\x89PNG")


def test_detail_tiles_first_and_partial_keeps_old():
    cfg = {**appsettings.DEFAULTS["tomorrow"], "enabled": True, "api_key": "k", "max_points": 1, "rain_tiles": True}
    t, calls = _adapter(cfg)
    t.budget = tio.Budget(hour_limit=1 + 6, day_limit=100)  # kuota hanya cukup untuk titik + tile detail
    t.refresh()
    tiles = [c for c in calls if "/map/tile/" in c]
    assert all("/map/tile/7/" in c for c in tiles[:6])
    v = t.view()["tiles"]["layers"]
    assert v[1]["zoom"] == 7 and v[1]["available"] == 6 and v[0]["available"] == 0


def _adapter(cfg, data_dir=None):
    db = types.SimpleNamespace()
    ctx = types.SimpleNamespace(db=db, index=None)
    if data_dir is not None:
        ctx.settings = types.SimpleNamespace(data_dir=data_dir)
    t = tio.Tomorrow(ctx)
    t.cfg = lambda: cfg
    calls = []

    def fake_get(path, params, key, binary=False):
        if not t.budget.allow(1):
            raise tio.TomorrowError("Kuota aman Tomorrow.io tercapai; pengambilan ditunda.")
        t.budget.record()
        calls.append(path)
        if binary:
            return b"\x89PNG\r\n\x1a\n" + b"x"
        return {"timelines": {"hourly": HOURLY}}

    t._get = fake_get
    t._health = lambda: None
    return t, calls


def test_refresh_uses_schedule_and_serves_tiles():
    cfg = {**appsettings.DEFAULTS["tomorrow"], "enabled": True, "api_key": "k", "max_points": 3}
    t, calls = _adapter(cfg)
    t.refresh()
    assert len(calls) == 3 + 10
    t.refresh()                                           # masih segar: tidak ada panggilan baru
    assert len(calls) == 13
    v = t.view()
    assert [p["name"] for p in v["points"]] == ["Padang", "Bukittinggi", "Payakumbuh"]
    assert t.tile(7, 99, 64).startswith(b"\x89PNG") and t.tile(7, 1, 1) == tio.EMPTY_PNG


def test_disabled_adapter_is_not_called():
    t, calls = _adapter({**appsettings.DEFAULTS["tomorrow"], "enabled": False, "api_key": "k"})
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
