"""Pure-Python planar geometry on WGS84 lon/lat (GeoJSON order).

Cukup untuk skala distribusi (polygon kejadian/CAP berukuran kecil sampai
kabupaten). Jarak dihitung dengan proyeksi equirectangular lokal; galat
< 0,5 % pada lintang Sumatera Barat. Semantik "covers": titik pada batas
polygon dihitung masuk, titik di dalam lubang (hole) tidak.
"""
from __future__ import annotations

import math
from typing import Iterable, Iterator, Sequence

EPS = 1e-12
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0

Pt = Sequence[float]


class GeometryError(ValueError):
    pass


# ----------------------------------------------------------------- utils
def iter_positions(geom: dict) -> Iterator[Pt]:
    t = geom.get("type")
    if t == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from iter_positions(g)
        return
    depth = {"Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2,
             "Polygon": 2, "MultiPolygon": 3}.get(t)
    if depth is None:
        return

    def walk(v, d):
        if d == 0:
            yield v
        else:
            for x in v:
                yield from walk(x, d - 1)

    yield from walk(geom.get("coordinates", []), depth)


def bbox_of(geom: dict) -> list[float]:
    xs, ys = [], []
    for p in iter_positions(geom):
        xs.append(p[0])
        ys.append(p[1])
    if not xs:
        raise GeometryError("Geometri kosong.")
    return [min(xs), min(ys), max(xs), max(ys)]


def bbox_intersects(a: Sequence[float], b: Sequence[float], pad_deg: float = 0.0) -> bool:
    return not (a[2] + pad_deg < b[0] or b[2] + pad_deg < a[0] or a[3] + pad_deg < b[1] or b[3] + pad_deg < a[1])


def meters_to_deg(m: float, lat: float) -> float:
    """Conservative degree padding for a distance in metres."""
    return m / (M_PER_DEG_LON_EQ * max(math.cos(math.radians(lat)), 0.2))


def polygons_of(geom: dict) -> list[list[list[Pt]]]:
    t = geom.get("type")
    if t == "Polygon":
        return [geom["coordinates"]]
    if t == "MultiPolygon":
        return list(geom["coordinates"])
    if t == "GeometryCollection":
        out = []
        for g in geom.get("geometries", []):
            out.extend(polygons_of(g))
        return out
    return []


def lines_of(geom: dict) -> list[list[Pt]]:
    t = geom.get("type")
    if t == "LineString":
        return [geom["coordinates"]]
    if t == "MultiLineString":
        return list(geom["coordinates"])
    if t == "GeometryCollection":
        out = []
        for g in geom.get("geometries", []):
            out.extend(lines_of(g))
        return out
    return []


def points_of(geom: dict) -> list[Pt]:
    t = geom.get("type")
    if t == "Point":
        return [geom["coordinates"]]
    if t == "MultiPoint":
        return list(geom["coordinates"])
    if t == "GeometryCollection":
        out = []
        for g in geom.get("geometries", []):
            out.extend(points_of(g))
        return out
    return []


# ------------------------------------------------------- point / polygon
def _on_segment(px, py, ax, ay, bx, by) -> bool:
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    scale = max(abs(bx - ax), abs(by - ay), 1e-9)
    if abs(cross) > EPS * scale * 1e3:
        return False
    return (min(ax, bx) - EPS <= px <= max(ax, bx) + EPS) and (min(ay, by) - EPS <= py <= max(ay, by) + EPS)


def point_in_ring(x: float, y: float, ring: Sequence[Pt]) -> int:
    """1 = inside, 0 = outside, -1 = on boundary."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if _on_segment(x, y, xi, yi, xj, yj):
            return -1
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return 1 if inside else 0


def polygon_covers_point(poly: Sequence[Sequence[Pt]], pt: Pt) -> bool:
    x, y = pt[0], pt[1]
    outer = point_in_ring(x, y, poly[0])
    if outer == 0:
        return False
    if outer == -1:
        return True
    for hole in poly[1:]:
        h = point_in_ring(x, y, hole)
        if h == 1:
            return False
        if h == -1:
            return True
    return True


def covers_point(area_geom: dict, pt: Pt) -> bool:
    return any(polygon_covers_point(p, pt) for p in polygons_of(area_geom))


# ---------------------------------------------------------- segments
def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    d1 = _orient(c[0], c[1], d[0], d[1], a[0], a[1])
    d2 = _orient(c[0], c[1], d[0], d[1], b[0], b[1])
    d3 = _orient(a[0], a[1], b[0], b[1], c[0], c[1])
    d4 = _orient(a[0], a[1], b[0], b[1], d[0], d[1])
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return (_on_segment(a[0], a[1], c[0], c[1], d[0], d[1]) or _on_segment(b[0], b[1], c[0], c[1], d[0], d[1])
            or _on_segment(c[0], c[1], a[0], a[1], b[0], b[1]) or _on_segment(d[0], d[1], a[0], a[1], b[0], b[1]))


def _proj(p: Pt, lat0: float) -> tuple[float, float]:
    return p[0] * M_PER_DEG_LON_EQ * math.cos(math.radians(lat0)), p[1] * M_PER_DEG_LAT


def _pt_seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def segment_distance_m(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    lat0 = (a[1] + b[1] + c[1] + d[1]) / 4
    A, B, C, D = (_proj(p, lat0) for p in (a, b, c, d))
    return min(_pt_seg_dist(A, C, D), _pt_seg_dist(B, C, D), _pt_seg_dist(C, A, B), _pt_seg_dist(D, A, B))


def _rings(poly):
    for ring in poly:
        for i in range(len(ring) - 1):
            yield ring[i], ring[i + 1]


def line_intersects_area(line: Sequence[Pt], area_geom: dict, corridor_m: float = 0.0) -> bool:
    if not line:
        return False
    polys = polygons_of(area_geom)
    for poly in polys:
        pb = bbox_of({"type": "Polygon", "coordinates": poly})
        pad = meters_to_deg(corridor_m, pb[1]) * 1.2 if corridor_m > 0 else 1e-9
        # Segments that can possibly touch this polygon (bbox prefilter).
        segs = []
        for a, b in (zip(line[:-1], line[1:]) if len(line) > 1 else [(line[0], line[0])]):
            if (max(a[0], b[0]) + pad < pb[0] or min(a[0], b[0]) - pad > pb[2]
                    or max(a[1], b[1]) + pad < pb[1] or min(a[1], b[1]) - pad > pb[3]):
                continue
            segs.append((a, b))
        if not segs:
            continue
        for a, _b in segs:
            if polygon_covers_point(poly, a):
                return True
        if polygon_covers_point(poly, segs[-1][1]):
            return True
        edges = list(_rings(poly))
        for a, b in segs:
            sb = (min(a[0], b[0]) - pad, min(a[1], b[1]) - pad, max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)
            for c, d in edges:
                if max(c[0], d[0]) < sb[0] or min(c[0], d[0]) > sb[2] or max(c[1], d[1]) < sb[1] or min(c[1], d[1]) > sb[3]:
                    continue
                if corridor_m > 0:
                    if segment_distance_m(a, b, c, d) <= corridor_m:
                        return True
                elif segments_intersect(a, b, c, d):
                    return True
    return False


def point_within_distance(pt: Pt, area_geom: dict, corridor_m: float) -> bool:
    if covers_point(area_geom, pt):
        return True
    if corridor_m <= 0:
        return False
    for poly in polygons_of(area_geom):
        for c, d in _rings(poly):
            if segment_distance_m(pt, pt, c, d) <= corridor_m:
                return True
    return False


def geom_intersects_area(geom: dict, area_geom: dict, corridor_m: float = 0.0,
                         point_corridor: bool = False) -> bool:
    """Exposure test of an asset geometry against a warning/impact area.

    Points use covers semantics (boundary counts). Lines are tested against
    the area optionally enlarged by a corridor (metres).
    """
    for p in points_of(geom):
        if point_within_distance(p, area_geom, corridor_m if point_corridor else 0.0):
            return True
    for ln in lines_of(geom):
        if line_intersects_area(ln, area_geom, corridor_m):
            return True
    for poly in polygons_of(geom):
        if any(line_intersects_area(r, area_geom, corridor_m) for r in poly):
            return True
        asset_poly = {"type": "Polygon", "coordinates": poly}
        if any(covers_point(asset_poly, ap[0][0]) for ap in polygons_of(area_geom)):
            return True
    return False


# ----------------------------------------------------------- builders
def circle_polygon(lon: float, lat: float, radius_km: float, steps: int = 64) -> dict:
    ring = []
    dlat = radius_km * 1000 / M_PER_DEG_LAT
    dlon = radius_km * 1000 / (M_PER_DEG_LON_EQ * max(math.cos(math.radians(lat)), 1e-6))
    for i in range(steps):
        a = 2 * math.pi * i / steps
        ring.append([round(lon + dlon * math.sin(a), 7), round(lat + dlat * math.cos(a), 7)])
    ring.append(list(ring[0]))
    return {"type": "Polygon", "coordinates": [ring]}


def ring_area_m2(ring: Sequence[Pt]) -> float:
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    pts = [_proj(p, lat0) for p in ring]
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(s) / 2


def area_km2(geom: dict) -> float:
    total = 0.0
    for poly in polygons_of(geom):
        total += ring_area_m2(poly[0]) - sum(ring_area_m2(h) for h in poly[1:])
    return total / 1e6


# --------------------------------------------------------- validation
def _check_pos(p) -> list[float]:
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        raise GeometryError("Koordinat tidak lengkap.")
    x, y = p[0], p[1]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)) or abs(x) > 180 or abs(y) > 90:
        raise GeometryError("Koordinat harus WGS84 [bujur, lintang].")
    return [float(x), float(y)]


def _ring_self_intersects(ring: Sequence[Pt]) -> bool:
    n = len(ring) - 1
    segs = [(ring[i], ring[i + 1]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if segments_intersect(*segs[i], *segs[j]):
                return True
    return False


def validate_area(geom: dict, max_vertices: int = 5000) -> dict:
    """Validate and normalise a user-drawn Polygon/MultiPolygon."""
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise GeometryError("Area harus berupa Polygon atau MultiPolygon.")
    polys = [geom.get("coordinates")] if geom["type"] == "Polygon" else geom.get("coordinates")
    if not isinstance(polys, list) or not polys:
        raise GeometryError("Polygon kosong.")
    out, count = [], 0
    for poly in polys:
        if not isinstance(poly, list) or not poly:
            raise GeometryError("Polygon kosong.")
        rings = []
        for ring in poly:
            if not isinstance(ring, list):
                raise GeometryError("Cincin polygon tidak valid.")
            r = [_check_pos(p) for p in ring]
            if len(r) >= 3 and r[0] != r[-1]:
                r.append(list(r[0]))
            if len(r) < 4:
                raise GeometryError("Polygon membutuhkan minimal tiga titik sudut.")
            count += len(r)
            if count > max_vertices:
                raise GeometryError(f"Polygon melebihi {max_vertices} titik sudut.")
            if _ring_self_intersects(r):
                raise GeometryError("Sisi polygon saling berpotongan (self-intersection). Perbaiki bentuknya.")
            if ring_area_m2(r) <= 1.0:
                raise GeometryError("Luas polygon terlalu kecil atau nol.")
            rings.append(r)
        for h in rings[1:]:
            if point_in_ring(h[0][0], h[0][1], rings[0]) == 0:
                raise GeometryError("Lubang polygon berada di luar batas luar.")
        out.append(rings)
    if len(out) == 1:
        return {"type": "Polygon", "coordinates": out[0]}
    return {"type": "MultiPolygon", "coordinates": out}


def validate_asset_geometry(g: dict) -> None:
    if not isinstance(g, dict):
        raise GeometryError("Geometri aset kosong.")
    t = g.get("type")
    if t == "GeometryCollection":
        if not g.get("geometries"):
            raise GeometryError("Geometri kosong.")
        for x in g["geometries"]:
            validate_asset_geometry(x)
        return
    depth = {"Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2, "Polygon": 2, "MultiPolygon": 3}
    if t not in depth:
        raise GeometryError("Jenis geometri belum didukung.")

    def chk(v, d):
        if not isinstance(v, list) or not v:
            raise GeometryError("Koordinat kosong.")
        if d == 0:
            _check_pos(v)
        else:
            for x in v:
                chk(x, d - 1)

    chk(g.get("coordinates"), depth[t])
    if t == "LineString" and len(g["coordinates"]) < 2:
        raise GeometryError("Ruas jaringan membutuhkan minimal dua koordinat.")
    if t == "MultiLineString" and any(len(l) < 2 for l in g["coordinates"]):
        raise GeometryError("Ruas jaringan membutuhkan minimal dua koordinat.")
    rings = g["coordinates"] if t == "Polygon" else [r for p in g["coordinates"] for r in p] if t == "MultiPolygon" else []
    if any(len(r) < 4 or list(r[0]) != list(r[-1]) for r in rings):
        raise GeometryError("Cincin polygon harus tertutup dengan minimal empat koordinat.")


# ----------------------------------------------------------- simplify
def simplify_line(coords: Sequence[Pt], tol_m: float) -> list[list[float]]:
    if len(coords) <= 2 or tol_m <= 0:
        return [[round(c[0], 6), round(c[1], 6)] for c in coords]
    lat0 = coords[0][1]
    pts = [_proj(c, lat0) for c in coords]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        s, e = stack.pop()
        best, idx = -1.0, -1
        for i in range(s + 1, e):
            d = _pt_seg_dist(pts[i], pts[s], pts[e])
            if d > best:
                best, idx = d, i
        if best > tol_m and idx > 0:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [[round(c[0], 6), round(c[1], 6)] for c, k in zip(coords, keep) if k]


def simplify_geometry(geom: dict, tol_m: float) -> dict:
    t = geom.get("type")
    if t == "LineString":
        return {"type": t, "coordinates": simplify_line(geom["coordinates"], tol_m)}
    if t == "MultiLineString":
        return {"type": t, "coordinates": [simplify_line(l, tol_m) for l in geom["coordinates"]]}
    if t == "Polygon":
        return {"type": t, "coordinates": [_simplify_ring(r, tol_m) for r in geom["coordinates"]]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[_simplify_ring(r, tol_m) for r in p] for p in geom["coordinates"]]}
    return geom


def _simplify_ring(r, tol):
    s = simplify_line(r, tol)
    if len(s) < 4:
        return [[round(c[0], 6), round(c[1], 6)] for c in r]
    return s


def centroid(geom: dict) -> tuple[float, float]:
    b = bbox_of(geom)
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def mean_lat(points: Iterable[Pt]) -> float:
    ys = [p[1] for p in points]
    return sum(ys) / len(ys) if ys else 0.0
