"""Impact analysis: gardu/trafo and customers inside one or more areas (PRD §7)."""
from __future__ import annotations

import datetime as dt

from . import geo
from .db import J

RULE_VERSION = "dic-covers-v1"
OTHER_POINT_KINDS = ("gi", "gh", "recloser", "lbs", "pltd", "kantor")


def load_customer_counts(db) -> tuple[int | None, dict[int, int | None]]:
    s = db.one("SELECT id FROM customer_count_sets WHERE active ORDER BY id DESC LIMIT 1")
    if not s:
        return None, {}
    rows = db.all("SELECT asset_id, count FROM customer_counts WHERE set_id = %s", (s["id"],))
    return s["id"], {r["asset_id"]: r["count"] for r in rows}


def compute(index, areas: list[dict], customers: dict[int, int | None], customer_set_id: int | None,
            max_list: int = 3000) -> dict:
    """areas: [{'id':..,'revision':..,'name':..,'geometry':{...}}] — geometry already validated."""
    if not areas:
        raise ValueError("Pilih minimal satu area.")
    matched: dict[int, dict] = {}
    others: dict[int, dict] = {}
    per_area = []
    for a in areas:
        g = a["geometry"]
        bb = geo.bbox_of(g)
        n = 0
        for r in index.points_in_bbox(bb, kinds=("trafo",) + OTHER_POINT_KINDS):
            if geo.covers_point(g, (r["lon"], r["lat"])):
                if r["kind"] == "trafo":
                    matched[r["id"]] = r
                    n += 1
                else:
                    others[r["id"]] = r
        per_area.append({"id": a.get("id"), "revision": a.get("revision"), "name": a.get("name"), "trafo": n,
                         "area_km2": round(geo.area_km2(g), 3)})

    lines = []
    for r in index.by_kind.get("jtm", []):
        if not any(geo.bbox_intersects(r["bbox"], geo.bbox_of(a["geometry"])) for a in areas):
            continue
        if any(geo.geom_intersects_area(r["geometry"], a["geometry"]) for a in areas):
            lines.append({"id": r["id"], "name": r["name"], "up3": r["up3"]})

    known = with_data = missing = 0
    cap = 0.0
    feeders: dict[str, dict] = {}
    gardu_ids = set()
    items = []
    for aid, r in sorted(matched.items(), key=lambda kv: ((kv[1]["feeder"] or ""), kv[1]["name"])):
        c = customers.get(aid) if customers else None
        has = c is not None
        if has:
            known += c
            with_data += 1
        else:
            missing += 1
        cap += r["capacity_kva"] or 0
        gardu_ids.add(r["gardu_id"] or f"asset:{aid}")
        f = feeders.setdefault(r["feeder"] or "(tanpa penyulang)", {"feeder": r["feeder"] or "(tanpa penyulang)",
                                                                   "up3": r["up3"], "trafo": 0, "customers_known": 0,
                                                                   "missing": 0, "kva": 0.0})
        f["trafo"] += 1
        f["kva"] += r["capacity_kva"] or 0
        if has:
            f["customers_known"] += c
        else:
            f["missing"] += 1
        items.append({"id": aid, "name": r["name"], "feeder": r["feeder"], "up3": r["up3"], "ulp": r["ulp"],
                      "gardu_id": r["gardu_id"], "kva": r["capacity_kva"], "customers": c,
                      "lon": r["lon"], "lat": r["lat"]})

    total = len(matched)
    complete = total > 0 and missing == 0
    if customer_set_id is None:
        scope = "Data pelanggan belum diunggah, sehingga hanya jumlah gardu/trafo yang dapat dihitung"
    elif complete:
        scope = "Pelanggan pada gardu terpilih (potensi cakupan dampak); data lengkap"
    else:
        scope = "Pelanggan pada gardu terpilih (potensi cakupan dampak); data sebagian"
    others_list = sorted(({"id": r["id"], "kind": r["kind"], "name": r["name"]} for r in others.values()),
                         key=lambda x: (x["kind"], x["name"]))
    return {
        "rule_version": RULE_VERSION,
        "asset_snapshot_id": index.snapshot_id,
        "customer_snapshot_id": customer_set_id,
        "areas": per_area,
        "trafo_count": total,
        "gardu_unique_count": len(gardu_ids),
        "known_customer_count": known,
        "gardu_with_customer_data": with_data,
        "gardu_missing_customer_data": missing,
        "customer_total_complete": complete,
        "capacity_kva_total": round(cap, 1),
        "service_state": "not_connected",
        "service_state_label": "Status nyala/padam belum tersedia (konektor Marco ditunda)",
        "outage_customer_count": None,
        "scope_label": scope,
        "feeders": sorted(feeders.values(), key=lambda f: (-f["trafo"], f["feeder"])),
        "lines_intersecting": lines,
        "other_assets": others_list,
        "items": items[:max_list],
        "items_truncated": len(items) > max_list,
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "_all_ids": [(i["id"], i["customers"]) for i in items],
    }


def save(db, result: dict, area_refs: list[dict], incident_id: int | None, actor: str, all_ids: list[tuple]) -> int:
    with db.tx() as t:
        aid = t.one("INSERT INTO impact_analyses(incident_id, area_refs, asset_snapshot_id, customer_set_id, rule_version,"
                    " computed_by, result) VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb) RETURNING id",
                    (incident_id, J(area_refs), result.get("asset_snapshot_id"), result.get("customer_snapshot_id"),
                     result["rule_version"], actor, J(result)))["id"]
        for s in range(0, len(all_ids), 1000):
            chunk = all_ids[s:s + 1000]
            t.run("INSERT INTO impact_analysis_assets(analysis_id, asset_id, known_customer_count) VALUES " +
                  ",".join(["(%s,%s,%s)"] * len(chunk)), [x for a, c in chunk for x in (aid, a, c)])
    return aid
