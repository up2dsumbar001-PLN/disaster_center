"""Unit tests: CAP filtering, exposure geometry, validation, analysis rules (PRD §7)."""
import datetime as dt

import pytest

from dcc import analysis, cap, geo
from dcc.assets import AssetIndex
from dcc.collector import compute_exposure

AT = dt.datetime(2026, 10, 28, 4, 0, tzinfo=dt.timezone.utc).timestamp()
SQUARE = {"type": "Polygon", "coordinates": [[[100, -1], [101, -1], [101, 0], [100, 0], [100, -1]]]}


def warning(**kw):
    base = dict(id="a", status="Actual", msg_type="Alert", references=[], sent="2026-10-28T03:00:00Z",
                effective="2026-10-28T03:00:00Z", expires="2026-10-28T05:00:00Z", areas=[SQUARE])
    base.update(kw)
    return cap.Warning(**base)


def index_of(rows):
    idx = AssetIndex()
    for i, r in enumerate(rows, start=1):
        r.setdefault("id", i)
        r.setdefault("name", f"A{i}")
        r.setdefault("feeder", None)
        r.setdefault("up3", None)
        r.setdefault("ulp", None)
        r.setdefault("gardu_id", None)
        r.setdefault("capacity_kva", None)
        g = r["geometry"]
        r["bbox"] = geo.bbox_of(g)
        if g["type"] == "Point":
            r["lon"], r["lat"] = g["coordinates"]
        else:
            r["lon"], r["lat"] = geo.centroid(g)
        idx.by_id[r["id"]] = r
        idx.by_kind.setdefault(r["kind"], []).append(r)
        if g["type"] == "Point":
            idx.grid.setdefault((int(r["lon"] // 0.05), int(r["lat"] // 0.05)), []).append(r)
    return idx


def pt(x, y, kind="trafo", **kw):
    return {"kind": kind, "geometry": {"type": "Point", "coordinates": [x, y]}, **kw}


def test_only_active_actual_alerts_survive():
    lst = [warning(), warning(id="expired", expires="2026-10-28T04:00:00Z"),
           warning(id="future", effective="2026-10-28T04:01:00Z"), warning(id="test", status="Test"),
           warning(id="ack", msg_type="Ack")]
    assert [w.id for w in cap.active_warnings(lst, AT)] == ["a"]


def test_cancel_and_update_suppress_references():
    assert cap.active_warnings([warning(), warning(id="cancel", msg_type="Cancel", references=["a"])], AT) == []
    assert [w.id for w in cap.active_warnings([warning(), warning(id="u", msg_type="Update", references=["a"])], AT)] == ["u"]
    assert cap.active_warnings([warning(), warning(id="u", msg_type="Update", references=["a"],
                                                   effective="2026-10-28T04:01:00Z")], AT) == []
    assert [w.id for w in cap.active_warnings([warning(), warning(id="tc", status="Test", msg_type="Cancel",
                                                                  references=["a"])], AT)] == ["a"]


def test_inside_outside_boundary():
    idx = index_of([pt(100.5, -.5), pt(102, -.5), pt(100, -.5)])
    assert compute_exposure(idx, warning(), 0) == [1, 3]


def test_crossing_feeder_detected():
    idx = index_of([{"kind": "jtm", "geometry": {"type": "LineString", "coordinates": [[99, -.5], [102, -.5]]}}])
    assert compute_exposure(idx, warning(), 0) == [1]


def test_corridor_catches_adjacent_line():
    line = {"kind": "jtm", "geometry": {"type": "LineString", "coordinates": [[99.9995, -.8], [99.9995, -.2]]}}
    assert compute_exposure(index_of([dict(line)]), warning(), 0) == []
    assert compute_exposure(index_of([dict(line)]), warning(), 100) == [1]


def test_holes_excluded():
    holed = {"type": "Polygon", "coordinates": SQUARE["coordinates"] + [[[100.4, -.6], [100.6, -.6], [100.6, -.4],
                                                                      [100.4, -.4], [100.4, -.6]]]}
    assert compute_exposure(index_of([pt(100.5, -.5)]), warning(areas=[holed]), 0) == []
    assert geo.covers_point(holed, (100.4, -.5))  # on hole boundary = on polygon boundary


def test_asset_validation():
    with pytest.raises(geo.GeometryError, match="Koordinat"):
        geo.validate_asset_geometry({"type": "Point", "coordinates": [-0.9, 100.3]})
    with pytest.raises(geo.GeometryError, match="dua"):
        geo.validate_asset_geometry({"type": "LineString", "coordinates": [[100, -1]]})
    with pytest.raises(geo.GeometryError, match="tertutup"):
        geo.validate_asset_geometry({"type": "Polygon", "coordinates": [[[100, -1], [101, -1], [101, 0]]]})


CAP_XML = ('<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>cap-1</identifier><sent>2026-10-28T03:00:00Z</sent>'
           '<status>Actual</status><msgType>Alert</msgType><info><language>en-US</language><headline>English</headline></info>'
           '<info><language>id-ID</language><headline>Hujan Lebat</headline><effective>2026-10-28T03:00:00Z</effective>'
           '<expires>2026-10-28T05:00:00Z</expires><area><areaDesc>Area pertama</areaDesc><polygon>-1,100 -1,101 0,101 0,100 -1,100'
           '</polygon></area><area><areaDesc>Area kedua</areaDesc><circle>-0.9,100.3 5</circle></area></info></alert>')


def test_cap_namespace_language_and_order():
    w = cap.parse_cap(CAP_XML)
    assert w.title == "Hujan Lebat"
    assert len(w.areas) == 2
    assert w.areas[0]["coordinates"][0][0] == [100, -1]
    assert compute_exposure(index_of([pt(100.3, -.9)]), w, 0) == [1]


def test_cancel_without_info():
    c = cap.parse_cap('<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>cancel</identifier>'
                      '<sent>2026-10-28T03:30:00Z</sent><status>Actual</status><msgType>Cancel</msgType>'
                      '<references>sender,a,2026-10-28T03:00:00Z</references></alert>')
    assert cap.active_warnings([warning(), c], AT) == []


def test_feed_filters_sumbar():
    rss = ('<rss><channel><item><title>Peringatan Dini Cuaca Sumatera Barat</title><link>https://www.bmkg.go.id/alerts/nowcast/id/'
           'CBT20261028_alert.xml</link></item><item><title>Jawa Barat</title><link>x</link></item></channel></rss>')
    items = cap.feed_items(rss)
    assert len(items) == 1 and items[0]["link"].endswith("_alert.xml")


def test_dtd_rejected():
    with pytest.raises(cap.CapError):
        cap.parse_cap('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]><alert>&e;</alert>')


# ------------------------------------------------------------ analysis
A = {"type": "Polygon", "coordinates": [[[100.0, -1.0], [100.2, -1.0], [100.2, -0.8], [100.0, -0.8], [100.0, -1.0]]]}
B = {"type": "Polygon", "coordinates": [[[100.1, -1.0], [100.4, -1.0], [100.4, -0.8], [100.1, -0.8], [100.1, -1.0]]]}


def test_prd_union_example():
    # G1 only in A (100), G2 in A and B (150), G3 only in B (unknown)
    idx = index_of([pt(100.05, -0.9, id=1, name="G1", feeder="F1", capacity_kva=100),
                    pt(100.15, -0.9, id=2, name="G2", feeder="F1", capacity_kva=160),
                    pt(100.3, -0.9, id=3, name="G3", feeder="F2", capacity_kva=50),
                    pt(101.0, -0.9, id=4, name="OUT")])
    res = analysis.compute(idx, [{"id": 10, "geometry": A}, {"id": 11, "geometry": B}], {1: 100, 2: 150, 3: None}, 7)
    assert res["trafo_count"] == 3
    assert res["known_customer_count"] == 250
    assert res["gardu_with_customer_data"] == 2
    assert res["gardu_missing_customer_data"] == 1
    assert res["customer_total_complete"] is False
    assert res["outage_customer_count"] is None
    assert "sebagian" in res["scope_label"]


def test_zero_is_not_missing_and_boundary_counts():
    idx = index_of([pt(100.0, -0.9, id=1), pt(100.2, -1.0, id=2)])  # both on A's boundary
    res = analysis.compute(idx, [{"id": 1, "geometry": A}], {1: 0, 2: 5}, 1)
    assert res["trafo_count"] == 2 and res["known_customer_count"] == 5 and res["customer_total_complete"] is True


def test_no_customer_set():
    idx = index_of([pt(100.05, -0.9)])
    res = analysis.compute(idx, [{"id": 1, "geometry": A}], {}, None)
    assert res["gardu_missing_customer_data"] == 1 and "belum diunggah" in res["scope_label"]


def test_validate_area_rejects_bowtie_and_closes_ring():
    with pytest.raises(geo.GeometryError, match="berpotongan"):
        geo.validate_area({"type": "Polygon", "coordinates": [[[100, -1], [101, 0], [101, -1], [100, 0], [100, -1]]]})
    g = geo.validate_area({"type": "Polygon", "coordinates": [[[100, -1], [101, -1], [101, 0], [100, 0]]]})
    assert g["coordinates"][0][0] == g["coordinates"][0][-1]
    assert abs(geo.area_km2(g) - 12290) < 200


def test_identical_republished_alert_shows_once_latest():
    # Kasus nyata 29 Okt 2026: BMKG menerbitkan ulang isi sama dengan ID baru, tipe Alert, tanpa referensi.
    a = warning(id="2.49.0.1.360.0.2026.10.29.09.13.003", title="Hujan Lebat disertai Petir di Sumatera Barat")
    b = warning(id="2.49.0.1.360.0.2026.10.29.10.13.003", title="Hujan Lebat disertai Petir di Sumatera Barat")
    out = cap.active_warnings([b, a], AT)
    assert [w.id for w in out] == [b.id] and out[0].republished == [a.id]


def test_different_content_alerts_all_shown():
    a = warning(id="x1", description="Hujan lebat di Padang")
    b = warning(id="x2", description="Hujan lebat di Padang dan Pariaman")
    c = warning(id="x3", description="Hujan lebat di Padang", expires="2026-10-28T06:00:00Z")
    d = warning(id="x4", description="Hujan lebat di Padang",
                areas=[{"type": "Polygon", "coordinates": [[[100, -1], [100.5, -1], [100.5, 0], [100, -1]]]}])
    assert sorted(w.id for w in cap.active_warnings([a, b, c, d], AT)) == ["x1", "x2", "x3", "x4"]
