"""Asset persistence (snapshots, upsert) and the in-memory spatial index."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import geo
from .db import J

POINT_KINDS = ("gi", "gh", "recloser", "lbs", "trafo", "pltd", "kantor", "lain")
DISPLAY_TOLERANCE_M = {"jtm": 8.0, "ulp": 40.0}
GRID = 0.05  # degrees (~5.5 km) for the point grid index


@dataclass
class AssetIndex:
    snapshot_id: int | None = None
    loaded_at: float = 0.0
    by_id: dict = field(default_factory=dict)
    by_kind: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)
    layer_cache: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def load(self, db) -> None:
        rows = db.all("SELECT id, kind, name, up3, ulp, feeder, gardu_id, code, capacity_kva, status, lon, lat, bbox, "
                      "geometry FROM assets WHERE active")
        snap = db.one("SELECT id FROM asset_snapshots WHERE state='published' ORDER BY id DESC LIMIT 1")
        by_id, by_kind, grid = {}, {}, {}
        for r in rows:
            by_id[r["id"]] = r
            by_kind.setdefault(r["kind"], []).append(r)
            if r["kind"] in POINT_KINDS and r["lon"] is not None:
                grid.setdefault((int(r["lon"] // GRID), int(r["lat"] // GRID)), []).append(r)
        with self.lock:
            self.by_id, self.by_kind, self.grid = by_id, by_kind, grid
            self.snapshot_id = snap["id"] if snap else None
            self.loaded_at = time.time()
            self.layer_cache = {}

    def points_in_bbox(self, bb, kinds=("trafo",)):
        out = []
        for gx in range(int(bb[0] // GRID), int(bb[2] // GRID) + 1):
            for gy in range(int(bb[1] // GRID), int(bb[3] // GRID) + 1):
                for r in self.grid.get((gx, gy), ()):
                    if r["kind"] in kinds and bb[0] <= r["lon"] <= bb[2] and bb[1] <= r["lat"] <= bb[3]:
                        out.append(r)
        return out

    def counts(self) -> dict:
        return {k: len(v) for k, v in self.by_kind.items()}

    # ---------------------------------------------------------- layers
    def layer(self, kind: str, up3: str | None = None) -> dict:
        key = (kind, up3)
        with self.lock:
            if key in self.layer_cache:
                return self.layer_cache[key]
        rows = [r for r in self.by_kind.get(kind, []) if not up3 or (r["up3"] or "").upper() == up3.upper()]
        if kind in POINT_KINDS:
            data = {"kind": kind, "type": "points", "count": len(rows),
                    "fields": ["id", "lon", "lat", "name", "feeder", "kva"],
                    "rows": [[r["id"], round(r["lon"], 6), round(r["lat"], 6), r["name"], r["feeder"],
                              r["capacity_kva"]] for r in rows]}
        else:
            tol = DISPLAY_TOLERANCE_M.get(kind, 10.0)
            feats = []
            for r in rows:
                feats.append({"type": "Feature", "id": r["id"],
                              "properties": {"id": r["id"], "name": r["name"], "up3": r["up3"], "feeder": r["feeder"]},
                              "geometry": geo.simplify_geometry(r["geometry"], tol)})
            data = {"kind": kind, "type": "geojson", "count": len(rows),
                    "geojson": {"type": "FeatureCollection", "features": feats}}
        with self.lock:
            self.layer_cache[key] = data
        return data


def publish_snapshot(db, records: list[dict], source: str, source_name: str, actor: str,
                     problems: list[str], snapshot_id: int | None = None) -> dict:
    """Upsert records; deactivate assets of the same source missing from a complete snapshot."""
    if snapshot_id is None:
        snapshot_id = db.one("INSERT INTO asset_snapshots(source, source_name, created_by) VALUES (%s,%s,%s) RETURNING id",
                             (source, source_name, actor))["id"]
    inserted = updated = 0
    try:
        with db.tx() as t:
            existing = {r["source_key"] for r in t.all("SELECT source_key FROM assets")}
            batch = []

            def flush():
                if not batch:
                    return
                values = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,true,now())"] * len(batch))
                params = []
                for r in batch:
                    params.extend([r["source_key"], r["kind"], r["name"][:300], r.get("up3"), r.get("ulp"), r.get("feeder"),
                                   r.get("gardu_id"), r.get("code"), r.get("capacity_kva"), r.get("status"),
                                   r["lon"], r["lat"], J(r["bbox"]), J(r["geometry"]), J(r.get("props") or {}),
                                   snapshot_id, snapshot_id])
                t.run("INSERT INTO assets(source_key,kind,name,up3,ulp,feeder,gardu_id,code,capacity_kva,status,lon,lat,"
                      "bbox,geometry,props,first_snapshot_id,last_snapshot_id,active,updated_at) VALUES " + values +
                      " ON CONFLICT (source_key) DO UPDATE SET kind=EXCLUDED.kind, name=EXCLUDED.name, up3=EXCLUDED.up3,"
                      " ulp=EXCLUDED.ulp, feeder=EXCLUDED.feeder, gardu_id=EXCLUDED.gardu_id, code=EXCLUDED.code,"
                      " capacity_kva=EXCLUDED.capacity_kva, status=EXCLUDED.status, lon=EXCLUDED.lon, lat=EXCLUDED.lat,"
                      " bbox=EXCLUDED.bbox, geometry=EXCLUDED.geometry, props=EXCLUDED.props,"
                      " last_snapshot_id=EXCLUDED.last_snapshot_id, active=true, updated_at=now()", params)
                batch.clear()

            for r in records:
                if r["source_key"] in existing:
                    updated += 1
                else:
                    inserted += 1
                batch.append(r)
                if len(batch) >= 400:
                    flush()
            flush()
            deactivated = t.run(
                "UPDATE assets SET active=false, updated_at=now() WHERE active AND last_snapshot_id <> %s AND "
                "first_snapshot_id IN (SELECT id FROM asset_snapshots WHERE source = %s)", (snapshot_id, source))
            counts = {"inserted": inserted, "updated": updated, "deactivated": max(deactivated, 0),
                      "total": len(records), "problems": len(problems)}
            by_kind: dict = {}
            for r in records:
                by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            counts["by_kind"] = by_kind
            t.run("UPDATE asset_snapshots SET state='published', completed_at=now(), counts=%s::jsonb, error=%s WHERE id=%s",
                  (J(counts), "\n".join(problems[:200]) or None, snapshot_id))
        return {"snapshot_id": snapshot_id, **counts}
    except Exception as exc:
        db.run("UPDATE asset_snapshots SET state='failed', completed_at=now(), error=%s WHERE id=%s",
               (str(exc)[:2000], snapshot_id))
        raise


def asset_detail(db, asset_id: int, include_customers: bool) -> dict | None:
    r = db.one("SELECT id, source_key, kind, name, up3, ulp, feeder, gardu_id, code, capacity_kva, status, lon, lat, "
               "props, active, updated_at, last_snapshot_id FROM assets WHERE id = %s", (asset_id,))
    if not r:
        return None
    if include_customers:
        c = db.one("SELECT cc.count, s.id AS set_id, s.uploaded_at, s.filename FROM customer_counts cc "
                   "JOIN customer_count_sets s ON s.id = cc.set_id WHERE s.active AND cc.asset_id = %s", (asset_id,))
        r["customers"] = c
    return r


def search(index: AssetIndex, q: str, kinds: tuple, limit: int = 30) -> list[dict]:
    q = q.strip().lower()
    if len(q) < 2:
        return []
    out = []
    for kind in kinds:
        for r in index.by_kind.get(kind, []):
            hay = f"{r['name']} {r.get('code') or ''} {r.get('gardu_id') or ''} {r.get('feeder') or ''}".lower()
            if q in hay:
                out.append({"id": r["id"], "kind": kind, "name": r["name"], "feeder": r["feeder"], "up3": r["up3"],
                            "lon": r["lon"], "lat": r["lat"], "bbox": r["bbox"]})
                if len(out) >= limit:
                    return out
    return out
