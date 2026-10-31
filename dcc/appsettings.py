"""Runtime settings editable by Admin (stored in app_settings, key 'config')."""
from __future__ import annotations

import copy
import threading

from .db import J

DEFAULTS: dict = {
    "corridor_m": 100,
    "basemaps": [
        {"id": "osm", "name": "OpenStreetMap", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
         "attribution": "&copy; OpenStreetMap contributors", "max_zoom": 19},
        {"id": "esri_img", "name": "Citra satelit (Esri)",
         "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
         "attribution": "Tiles &copy; Esri", "max_zoom": 19},
        {"id": "esri_topo", "name": "Topografi (Esri)",
         "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
         "attribution": "Tiles &copy; Esri", "max_zoom": 19},
    ],
    "guest": {"enabled": True, "layers": ["gi", "gh", "jtm", "ulp", "kantor"], "show_alerts": True,
              "show_incidents": True},
    "openmeteo": {"enabled": False, "api_key": ""},
    "rainviewer": {"enabled": False},
    "tomorrow": {"enabled": False, "api_key": "", "interval_min": 120, "max_points": 8, "rain_tiles": True},
    "windy": {"url": "https://www.windy.com/-0.95/100.35?radar,-0.95,100.35,8"},
    "wa": {
        "enabled": False,
        "url": "",
        "method": "POST",
        "headers": {},
        "body_template": "{\"number\": \"{{target}}\", \"message\": \"{{message}}\"}",
        "targets": [],
        "notify_bmkg": True,
        "notify_incident": True,
        "incident_min_severity": "tinggi",
        "app_url": "http://10.17.119.x:18085",
    },
    "wag": {"enabled": True, "url": "http://127.0.0.1:18083", "token": "", "group": "BMKGSUMBAR&Stakeholder"},
    "arcgis": {"enabled": False, "layers": []},
}

SECRET_PATHS = [("openmeteo", "api_key"), ("tomorrow", "api_key"), ("wa", "headers"), ("wag", "token")]
_lock = threading.Lock()
_cache: dict | None = None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "headers":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get(db) -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return copy.deepcopy(_cache)
    row = db.one("SELECT value FROM app_settings WHERE key = 'config'")
    cfg = _merge(DEFAULTS, row["value"] if row else {})
    with _lock:
        _cache = cfg
    return copy.deepcopy(cfg)


def put(db, patch: dict, actor: str) -> dict:
    global _cache
    current = get(db)
    # Keep secrets when the client sends the masked placeholder back.
    for a, b in SECRET_PATHS:
        if isinstance(patch.get(a), dict) and patch[a].get(b) == "********":
            patch[a][b] = current[a][b]
    if isinstance(patch.get("wa"), dict) and isinstance(patch["wa"].get("headers"), dict):
        old = current["wa"].get("headers") or {}
        patch["wa"]["headers"] = {k: (old.get(k, "") if v == "********" else v) for k, v in patch["wa"]["headers"].items()}
    new = _merge(current, patch)
    db.run("INSERT INTO app_settings(key, value, updated_by) VALUES ('config', %s::jsonb, %s) "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
           (J(new), actor))
    with _lock:
        _cache = new
    return copy.deepcopy(new)


def masked(cfg: dict) -> dict:
    c = copy.deepcopy(cfg)
    if c["openmeteo"].get("api_key"):
        c["openmeteo"]["api_key"] = "********"
    if c["tomorrow"].get("api_key"):
        c["tomorrow"]["api_key"] = "********"
    c["wa"]["headers"] = {k: "********" for k in (c["wa"].get("headers") or {})}
    if c["wag"].get("token"):
        c["wag"]["token"] = "********"
    return c


def public_view(cfg: dict) -> dict:
    return {"corridor_m": cfg["corridor_m"], "basemaps": cfg["basemaps"], "guest": cfg["guest"],
            "openmeteo": {"enabled": cfg["openmeteo"]["enabled"]}, "rainviewer": cfg["rainviewer"],
            "tomorrow": {"enabled": bool(cfg["tomorrow"].get("enabled") and cfg["tomorrow"].get("api_key")),
                         "rain_tiles": bool(cfg["tomorrow"].get("rain_tiles", True))},
            "windy": cfg["windy"], "wag": {"enabled": bool(cfg["wag"].get("enabled"))}}


def reset_cache():
    global _cache
    with _lock:
        _cache = None
