"""BMKG Common Alerting Protocol (CAP) parsing and active-alert filtering.

Port dari prototipe (lib/disaster-model.ts) dengan perilaku yang sama:
- memilih blok <info> berbahasa Indonesia bila ada;
- koordinat CAP "lat,lon" diubah menjadi GeoJSON [lon, lat];
- Update/Cancel berstatus Actual menekan peringatan yang dirujuk;
- Test/Exercise, kedaluwarsa dan belum berlaku tidak dianggap aktif.
"""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import geo

SUMBAR_RE = re.compile(r"sumat(?:e|ra)?(?:ra)?\s*barat|sumbar", re.I)


class CapError(ValueError):
    pass


@dataclass
class Warning:
    id: str
    title: str = ""
    description: str = ""
    instruction: str = ""
    severity: str = "Unknown"
    certainty: str = "Unknown"
    urgency: str = "Unknown"
    sent: str = ""
    effective: str = ""
    expires: str = ""
    status: str = ""
    msg_type: str = ""
    references: list = field(default_factory=list)
    areas: list = field(default_factory=list)       # list of GeoJSON geometries
    areas_text: list = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el, name):
    return [c for c in el.iter() if _local(c.tag) == name]


def _txt(el, name) -> str:
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_xml(text: str):
    try:
        # Disallow DTD/entity tricks: ElementTree does not resolve external entities.
        if "<!DOCTYPE" in text[:2000] or "<!ENTITY" in text[:5000]:
            raise CapError("XML dengan DTD tidak diterima.")
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise CapError("XML tidak valid.") from exc


def parse_cap(text: str) -> Warning:
    root = parse_xml(text)
    infos = _children(root, "info")
    info = next((i for i in infos if re.match(r"^id", _txt(i, "language"), re.I)), infos[0] if infos else None)
    ident = ""
    sent = ""
    status = ""
    msg_type = ""
    refs_txt = ""
    for c in root:
        n = _local(c.tag)
        if n == "identifier":
            ident = (c.text or "").strip()
        elif n == "sent":
            sent = (c.text or "").strip()
        elif n == "status":
            status = (c.text or "").strip()
        elif n == "msgType":
            msg_type = (c.text or "").strip()
        elif n == "references":
            refs_txt = (c.text or "").strip()
    if not ident or not sent:
        raise CapError("Identitas CAP tidak lengkap.")
    refs = []
    for part in refs_txt.split():
        bits = part.split(",")
        if len(bits) > 1 and bits[1]:
            refs.append(bits[1])
    w = Warning(id=ident, sent=sent, status=status, msg_type=msg_type, references=refs)
    if info is None:
        w.title = "Pembaruan peringatan"
        return w
    w.title = _txt(info, "headline") or _txt(info, "event")
    w.description = _txt(info, "description")
    w.instruction = _txt(info, "instruction")
    w.severity = _txt(info, "severity") or "Unknown"
    w.certainty = _txt(info, "certainty") or "Unknown"
    w.urgency = _txt(info, "urgency") or "Unknown"
    w.effective = _txt(info, "effective") or sent
    w.expires = _txt(info, "expires")
    for area in _children(info, "area"):
        w.areas_text.append(_txt(area, "areaDesc"))
        for p in _children(area, "polygon"):
            ring = []
            for pair in (p.text or "").split():
                try:
                    lat, lon = (float(v) for v in pair.split(",")[:2])
                except ValueError as exc:
                    raise CapError("Koordinat CAP tidak valid.") from exc
                if abs(lat) > 90 or abs(lon) > 180:
                    raise CapError("Koordinat CAP tidak valid.")
                ring.append([lon, lat])
            if len(ring) < 3:
                raise CapError("Polygon CAP tidak lengkap.")
            if ring[0] != ring[-1]:
                ring.append(list(ring[0]))
            w.areas.append({"type": "Polygon", "coordinates": [ring]})
        for c in _children(area, "circle"):
            parts = (c.text or "").split()
            try:
                lat, lon = (float(v) for v in parts[0].split(",")[:2])
                radius = float(parts[1])
            except (ValueError, IndexError) as exc:
                raise CapError("Lingkaran CAP tidak valid.") from exc
            if abs(lat) > 90 or abs(lon) > 180 or radius <= 0:
                raise CapError("Lingkaran CAP tidak valid.")
            w.areas.append(geo.circle_polygon(lon, lat, radius))
    return w


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        v = s.strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(v)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def active_warnings(all_w: list[Warning], at: float | None = None) -> list[Warning]:
    at = dt.datetime.now(dt.timezone.utc).timestamp() if at is None else at

    def le(s, now):
        t = parse_time(s)
        return t is not None and t <= now

    suppressed = set()
    for w in all_w:
        if w.status == "Actual" and w.msg_type in ("Update", "Cancel") and le(w.sent, at):
            suppressed.update(w.references)
    unique = {}
    for w in all_w:
        unique[w.id] = w
    out = []
    for w in unique.values():
        exp = parse_time(w.expires)
        if (w.status == "Actual" and w.msg_type in ("Alert", "Update") and w.id not in suppressed
                and le(w.sent, at) and le(w.effective, at) and exp is not None and exp > at):
            out.append(w)
    return out


def feed_items(rss_text: str) -> list[dict]:
    """Return [{'title','link','description','pubDate'}] for Sumatera Barat items."""
    root = parse_xml(rss_text)
    if _local(root.tag) not in ("rss", "feed") and not _children(root, "channel"):
        raise CapError("Feed peringatan tidak valid.")
    items = []
    for it in _children(root, "item"):
        title, desc, link = _txt(it, "title"), _txt(it, "description"), _txt(it, "link")
        if SUMBAR_RE.search(f"{title} {desc}"):
            items.append({"title": title, "link": link, "description": desc, "pubDate": _txt(it, "pubDate")})
    return items
