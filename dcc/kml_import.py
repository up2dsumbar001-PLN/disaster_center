"""Streaming KML/KMZ parser for the "ASET JARINGAN SUMBAR" Google Earth export.

Menghasilkan rekaman aset ternormalisasi. Mengenali struktur folder:
  ASET JARINGAN / GI SUMBAR | GH-GB SUMBAR | Recloser Sumbar | LBS Sumbar
                / Trafo Sumbar / <UP3> / <PENYULANG (n trafo)>
                / JTM Sumbar / JTM_<UP3>
  Batas_ULP_Local, PLTD Mentawai, Kantor Unit/...
Folder lain diklasifikasikan dengan kata kunci nama folder dan jenis geometri.
"""
from __future__ import annotations

import hashlib
import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import IO, Iterator

from . import geo

KML_NS = "{http://www.opengis.net/kml/2.2}"
NULLS = {"", "<null>", "null", "none", "-"}
TD_PAIR = re.compile(r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
FEEDER_COUNT = re.compile(r"\s*\(\d+\s*trafo\)\s*$", re.I)
GARDU_CODE = re.compile(r"\bGD\s+([A-Z]{2,4})[- ]([A-Z0-9]{2,4})[- ]([A-Z])[- ]?(\d{3,5})\b")

KIND_LABEL = {
    "gi": "Gardu Induk", "gh": "Gardu Hubung / GB", "recloser": "Recloser", "lbs": "LBS",
    "trafo": "Gardu Distribusi (Trafo)", "jtm": "Jaringan TM (Penyulang)", "ulp": "Batas ULP",
    "pltd": "PLTD", "kantor": "Kantor Unit", "lain": "Aset lain",
}


class KmlError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    v = html.unescape(TAG.sub("", html.unescape(v))).strip()
    return None if v.lower() in NULLS else v


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _coords(text: str | None) -> list[list[float]]:
    out = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        out.append([round(lon, 7), round(lat, 7)])
    return out


def _geom(el) -> dict | None:
    n = _local(el.tag)
    if n == "Point":
        c = _coords(_find_text(el, "coordinates"))
        return {"type": "Point", "coordinates": c[0]} if c else None
    if n == "LineString":
        c = _coords(_find_text(el, "coordinates"))
        return {"type": "LineString", "coordinates": c} if len(c) >= 2 else None
    if n == "LinearRing":
        c = _coords(_find_text(el, "coordinates"))
        return {"type": "Polygon", "coordinates": [c]} if len(c) >= 4 else None
    if n == "Polygon":
        rings = []
        for b in el:
            bn = _local(b.tag)
            if bn in ("outerBoundaryIs", "innerBoundaryIs"):
                for lr in b.iter():
                    if _local(lr.tag) == "LinearRing":
                        r = _coords(_find_text(lr, "coordinates"))
                        if len(r) >= 3 and r[0] != r[-1]:
                            r.append(list(r[0]))
                        if len(r) >= 4:
                            if bn == "outerBoundaryIs":
                                rings.insert(0, r)
                            else:
                                rings.append(r)
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if n == "MultiGeometry":
        parts = [g for g in (_geom(c) for c in el) if g]
        if not parts:
            return None
        types = {p["type"] for p in parts}
        if types == {"LineString"}:
            return {"type": "MultiLineString", "coordinates": [p["coordinates"] for p in parts]}
        if types == {"Polygon"}:
            return {"type": "MultiPolygon", "coordinates": [p["coordinates"] for p in parts]}
        if types == {"Point"}:
            return {"type": "MultiPoint", "coordinates": [p["coordinates"] for p in parts]}
        if types <= {"LineString", "MultiLineString"}:
            ls = []
            for p in parts:
                ls.extend([p["coordinates"]] if p["type"] == "LineString" else p["coordinates"])
            return {"type": "MultiLineString", "coordinates": ls}
        return {"type": "GeometryCollection", "geometries": parts}
    return None


def _find_text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return c.text
    return None


def _placemark_geometry(pm) -> dict | None:
    for c in pm:
        if _local(c.tag) in ("Point", "LineString", "Polygon", "MultiGeometry", "LinearRing"):
            return _geom(c)
    return None


def _attrs(pm, schema: dict) -> dict:
    out = {}
    for el in pm.iter():
        n = _local(el.tag)
        if n == "Data":
            key = el.get("name")
            val = _find_text(el, "value")
            if key:
                out[key] = _clean(val)
        elif n == "SimpleData":
            key = schema.get(el.get("name"), el.get("name"))
            if key:
                out[_clean(key) or el.get("name")] = _clean(el.text)
    desc = _find_text(pm, "description") or ""
    if "<td" in desc.lower() or "&lt;td" in desc.lower():
        d = html.unescape(desc) if "&lt;td" in desc.lower() else desc
        for k, v in TD_PAIR.findall(d):
            k2 = _clean(k)
            if k2 and k2 not in out:
                out[k2] = _clean(v)
    elif "<br" in desc.lower() or "&lt;br" in desc.lower():
        d = html.unescape(desc)
        for line in re.split(r"<br\s*/?>", d, flags=re.I):
            line = TAG.sub("", line).strip()
            m = re.match(r"^([A-Za-z_][\w ]{0,40}?)\s*[:]\s*(.*)$", line) or re.match(r"^([A-Z][A-Z0-9_]{1,30})\s+(.+)$", line)
            if m:
                k2, v2 = m.group(1).strip(), _clean(m.group(2))
                if k2 and k2 not in out and v2 is not None:
                    out[k2] = v2
    return {k: v for k, v in out.items() if v is not None}


def _classify(path: list[str], name: str, gtype: str) -> str:
    p = [x.lower() for x in path]
    joined = " / ".join(p)
    if any(x.startswith("trafo") or "gardu distribusi" in x for x in p):
        return "trafo"
    if any(x.startswith("jtm") or "penyulang" in x or "sutm" in x or "jaringan tm" in x for x in p) and "Line" in gtype:
        return "jtm"
    if any(x.startswith("recloser") for x in p):
        return "recloser"
    if any(x.startswith("lbs") for x in p):
        return "lbs"
    if any(x.startswith("gi ") or x == "gi" or "gardu induk" in x for x in p):
        return "gi"
    if any(x.startswith("gh") or "gardu hubung" in x for x in p):
        return "gh"
    if "batas" in joined and "Polygon" in gtype:
        return "ulp"
    if "pltd" in joined or "pembangkit" in joined:
        return "pltd"
    if "kantor" in joined:
        return "kantor"
    nm = name.lower()
    if "Line" in gtype:
        return "jtm"
    if re.match(r"^(rec|recloser)\b", nm):
        return "recloser"
    if re.match(r"^lbs", nm):
        return "lbs"
    if re.match(r"^gi\b", nm):
        return "gi"
    if re.match(r"^(gh|gb)\b", nm):
        return "gh"
    if "Polygon" in gtype:
        return "ulp"
    return "lain"


def _up3_from_path(path: list[str], kind: str) -> str | None:
    for x in reversed(path):
        m = re.match(r"^JTM[_ ](.+)$", x, re.I)
        if m:
            return m.group(1).strip().upper()
    if kind == "trafo":
        for i, x in enumerate(path):
            if x.lower().startswith("trafo") and i + 1 < len(path):
                return path[i + 1].strip().upper()
    return None


def _key(kind: str, *parts) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{h}"


def open_kml_stream(filename: str, data: bytes) -> IO[bytes]:
    if filename.lower().endswith(".kmz") or data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise KmlError("Berkas KMZ rusak.") from exc
        names = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise KmlError("KMZ tidak memuat berkas .kml.")
        name = "doc.kml" if "doc.kml" in names else names[0]
        info = z.getinfo(name)
        if info.file_size > 600 * 1024 * 1024:
            raise KmlError("Isi KMZ terlalu besar.")
        return io.BytesIO(z.read(name))
    return io.BytesIO(data)


def iter_placemarks(stream: IO[bytes]) -> Iterator[dict]:
    path: list[str | None] = []
    schema: dict[str, str] = {}
    head = stream.read(4096)
    stream.seek(0)
    if b"<!ENTITY" in head or b"<!DOCTYPE" in head:
        raise KmlError("KML dengan DTD/ENTITY tidak diterima.")
    stack: list[str] = []
    try:
        for ev, el in ET.iterparse(stream, events=("start", "end")):
            n = _local(el.tag)
            if ev == "start":
                stack.append(n)
                if n in ("Folder", "Document"):
                    path.append(None)
                continue
            stack.pop()
            if n == "name" and path and path[-1] is None and stack and stack[-1] in ("Folder", "Document"):
                path[-1] = (el.text or "").strip()
            elif n == "SimpleField":
                dn = _find_text(el, "displayName")
                if el.get("name"):
                    schema[el.get("name")] = _clean(dn) or el.get("name")
            elif n == "Placemark":
                g = _placemark_geometry(el)
                name = (_find_text(el, "name") or "").strip()
                if g:
                    yield {"path": [p for p in path if p], "name": html.unescape(name), "geometry": g,
                           "attrs": _attrs(el, schema)}
                el.clear()
            elif n in ("Folder", "Document"):
                path.pop()
                el.clear()
    except ET.ParseError as exc:
        raise KmlError(f"KML tidak valid: {exc}") from exc


def _ulp_props(attrs: dict, name: str) -> dict:
    out = {}
    for k in ("ULP", "UP3", "UNIT_INDUK", "Luas_KM2", "Rekap_Kab_Kota"):
        if k in attrs:
            out[k.lower()] = attrs[k]
    out.setdefault("ulp", name)
    return out


def normalise(pm: dict) -> dict:
    path, name, g, a = pm["path"], pm["name"], pm["geometry"], pm["attrs"]
    geo.validate_asset_geometry(g)
    kind = _classify(path, name, g["type"])
    rec = {"kind": kind, "name": name or KIND_LABEL.get(kind, "Aset"), "geometry": g, "up3": _up3_from_path(path, kind),
           "ulp": None, "feeder": None, "gardu_id": None, "code": None, "capacity_kva": None, "status": None,
           "props": {"folder": " / ".join(path)}}
    if g["type"] == "Point":
        rec["lon"], rec["lat"] = g["coordinates"][0], g["coordinates"][1]
    else:
        cx, cy = geo.centroid(g)
        rec["lon"], rec["lat"] = round(cx, 7), round(cy, 7)
    rec["bbox"] = [round(v, 7) for v in geo.bbox_of(g)]

    if kind == "trafo":
        folder = path[-1] if path else ""
        rec["feeder"] = FEEDER_COUNT.sub("", folder).strip() or None
        rec["gardu_id"] = a.get("LOCATION")
        m = GARDU_CODE.search(a.get("DESCRIPTION") or "")
        rec["code"] = "-".join(m.groups()) if m else None
        rec["capacity_kva"] = _num(a.get("KAPASITAS"))
        rec["status"] = a.get("STATUS")
        keep = ["GLOBALID", "globalid_1", "ASSETNUM", "LOCATION", "DESCRIPTION", "KAPASITAS", "FASA_TRAFO",
                "JENIS_TRAFO", "NO_TRAFO", "STATUS", "TUJDNUMBER", "MANUFACTURER", "TH_BUAT", "PERUNTUKAN",
                "STATUS_KEPEMILIKAN", "TEGANGAN_TRAFO", "KODE_HANTARAN", "OWNER_PEMELIHARAAN", "PRIORITAS",
                "last_edited_date", "last_edited_user", "INSTALLDATE", "NO_SLO", "PENYULANG"]
        rec["props"].update({k: a[k] for k in keep if a.get(k) is not None})
        ident = a.get("globalid_1") or a.get("GLOBALID") or a.get("ASSETNUM")
        rec["source_key"] = f"trafo:{ident}" if ident else _key("trafo", name, rec["lon"], rec["lat"])
    elif kind == "jtm":
        rec["feeder"] = name.strip() or None
        rec["props"].update({k: v for k, v in a.items() if len(str(v)) < 200})
        rec["source_key"] = _key("jtm", rec["up3"], name)
    elif kind == "ulp":
        rec["props"].update(_ulp_props(a, name))
        rec["ulp"] = name
        rec["up3"] = (a.get("UP3") or rec["up3"] or None)
        rec["source_key"] = _key("ulp", name)
    else:
        rec["props"].update({k: v for k, v in a.items() if len(str(v)) < 200 and not re.match(r"^-?\d+\.\d+$", k)})
        rec["source_key"] = _key(kind, name, rec["lon"], rec["lat"])
    return rec


def parse_assets(stream: IO[bytes], max_features: int = 200_000) -> tuple[list[dict], list[str]]:
    records, problems, seen = [], [], {}
    for i, pm in enumerate(iter_placemarks(stream)):
        if i >= max_features:
            raise KmlError(f"Jumlah fitur melebihi batas {max_features}.")
        try:
            rec = normalise(pm)
        except geo.GeometryError as exc:
            if len(problems) < 200:
                problems.append(f"{pm.get('name') or 'tanpa nama'}: {exc}")
            continue
        k = rec["source_key"]
        if k in seen:
            seen[k] += 1
            rec["source_key"] = f"{k}#{seen[k]}"
            if len(problems) < 200:
                problems.append(f"ID ganda {k} pada '{rec['name']}' — diberi akhiran #{seen[k]}")
        else:
            seen[k] = 1
        records.append(rec)
    if not records:
        raise KmlError("Tidak ada placemark dengan geometri yang valid.")
    assign_ulp(records)
    return records, problems


def assign_ulp(records: list[dict]) -> None:
    """Spatial join: give each point asset the ULP/UP3 whose boundary covers it."""
    ulps = []
    for r in records:
        if r["kind"] == "ulp":
            g = geo.simplify_geometry(r["geometry"], 25.0)
            ulps.append((r["bbox"], g, r["name"], r.get("up3")))
    if not ulps:
        return
    for r in records:
        if r["kind"] in ("ulp", "jtm") or r.get("ulp"):
            continue
        pt = (r["lon"], r["lat"])
        for bb, g, name, up3 in ulps:
            if bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3] and geo.covers_point(g, pt):
                r["ulp"] = name
                if not r.get("up3") and up3:
                    r["up3"] = str(up3).upper()
                break
