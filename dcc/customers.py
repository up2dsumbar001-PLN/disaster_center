"""Customer-count upload (CSV/XLSX) mapped to gardu/trafo assets."""
from __future__ import annotations

import csv
import io
import re

from .db import J

KEY_CANDIDATES = ["LOCATION", "ID_GARDU", "IDGARDU", "KODE_GARDU", "KD_GARDU", "GARDU", "ASSETNUM", "GLOBALID",
                  "GLOBALID_1", "NAMA_GARDU", "NAMA", "NAME"]
COUNT_CANDIDATES = ["JUMLAH_PELANGGAN", "JML_PELANGGAN", "JML_PLG", "PELANGGAN", "JUMLAH_PLG", "PLG", "CUSTOMERS",
                    "CUSTOMER_COUNT", "JUMLAH"]


class CustomerError(ValueError):
    pass


def _norm(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _hdr(s) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(s or "").strip().upper()).strip("_")


def read_table(filename: str, data: bytes) -> tuple[list[str], list[list]]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise CustomerError("Paket openpyxl belum terpasang.") from exc
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise CustomerError("Berkas Excel tidak dapat dibaca.") from exc
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")):
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    else:
        raise CustomerError("Gunakan berkas .xlsx atau .csv.")
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if len(rows) < 2:
        raise CustomerError("Tabel kosong atau hanya berisi judul kolom.")
    header = [_hdr(c) for c in rows[0]]
    return header, rows[1:]


def detect_columns(header: list[str], key_field: str | None, count_field: str | None) -> tuple[int, int]:
    def find(wanted, cands):
        if wanted:
            w = _hdr(wanted)
            if w in header:
                return header.index(w)
            raise CustomerError(f"Kolom '{wanted}' tidak ditemukan. Kolom tersedia: {', '.join(header)}")
        for c in cands:
            if c in header:
                return header.index(c)
        return -1

    k = find(key_field, KEY_CANDIDATES)
    c = find(count_field, COUNT_CANDIDATES)
    if k < 0 or c < 0:
        raise CustomerError("Kolom kunci gardu atau jumlah pelanggan tidak dikenali. Pilih nama kolom secara manual. "
                            f"Kolom tersedia: {', '.join(header)}")
    return k, c


def _parse_count(v):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, (int, float)):
        if v < 0 or v != v:
            raise ValueError("negatif")
        return int(round(v))
    s = str(v).strip().replace(".", "").replace(",", "") if re.fullmatch(r"\d{1,3}([.,]\d{3})+", str(v).strip()) else str(v).strip().replace(",", ".")
    f = float(s)
    if f < 0:
        raise ValueError("negatif")
    return int(round(f))


def build_lookup(db) -> dict[str, list[int]]:
    """Map normalised identifiers to trafo asset ids (a key may match several trafo)."""
    lookup: dict[str, list[int]] = {}
    rows = db.all("SELECT id, name, gardu_id, code, props->>'ASSETNUM' AS assetnum, props->>'GLOBALID' AS gid, "
                  "props->>'globalid_1' AS gid1 FROM assets WHERE active AND kind = 'trafo'")
    for r in rows:
        for v in (r["gardu_id"], r["code"], r["assetnum"], r["gid"], r["gid1"], r["name"]):
            n = _norm(v)
            if n:
                lookup.setdefault(n, [])
                if r["id"] not in lookup[n]:
                    lookup[n].append(r["id"])
    return lookup


def import_counts(db, filename: str, data: bytes, actor: str, key_field: str | None = None,
                  count_field: str | None = None, effective_date: str | None = None) -> dict:
    header, rows = read_table(filename, data)
    ki, ci = detect_columns(header, key_field, count_field)
    lookup = build_lookup(db)
    if not lookup:
        raise CustomerError("Belum ada data gardu/trafo. Impor aset terlebih dahulu.")
    counts: dict[int, int | None] = {}
    unmatched, bad, ambiguous, dup = [], [], [], 0
    for i, r in enumerate(rows, start=2):
        key = r[ki] if ki < len(r) else None
        raw = r[ci] if ci < len(r) else None
        ids = lookup.get(_norm(key))
        if not ids:
            if len(unmatched) < 50:
                unmatched.append({"baris": i, "kunci": str(key)})
            continue
        try:
            n = _parse_count(raw)
        except ValueError:
            if len(bad) < 50:
                bad.append({"baris": i, "kunci": str(key), "nilai": str(raw)})
            continue
        if len(ids) > 1:
            # One gardu key → several trafo: keep the total on the first trafo, others NULL
            # so the gardu is not double-counted in the analysis.
            if len(ambiguous) < 50:
                ambiguous.append({"baris": i, "kunci": str(key), "trafo": len(ids)})
        target = ids[0]
        if target in counts:
            dup += 1
            prev = counts[target]
            counts[target] = None if prev is None or n is None else prev + n
        else:
            counts[target] = n
    matched = len(counts)
    with db.tx() as t:
        set_id = t.one("INSERT INTO customer_count_sets(filename,key_field,count_field,uploaded_by,effective_date,rows_total,"
                       "rows_matched,rows_unmatched,unmatched_sample,active) VALUES (%s,%s,%s,%s,%s::date,%s,%s,%s,%s::jsonb,false) "
                       "RETURNING id",
                       (filename, header[ki], header[ci], actor, effective_date or None, len(rows), matched,
                        len(rows) - matched, J({"tidak_cocok": unmatched, "nilai_tidak_valid": bad,
                                                "kunci_banyak_trafo": ambiguous, "baris_ganda": dup})))["id"]
        items = list(counts.items())
        for s in range(0, len(items), 1000):
            chunk = items[s:s + 1000]
            t.run("INSERT INTO customer_counts(set_id, asset_id, count) VALUES " + ",".join(["(%s,%s,%s)"] * len(chunk)),
                  [x for aid, n in chunk for x in (set_id, aid, n)])
        t.run("UPDATE customer_count_sets SET active = (id = %s)", (set_id,))
    return {"set_id": set_id, "rows_total": len(rows), "rows_matched": matched, "rows_unmatched": len(rows) - matched,
            "key_field": header[ki], "count_field": header[ci], "unmatched_sample": unmatched[:10],
            "invalid_values": bad[:10], "duplicate_rows": dup}


def template_csv(db) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["LOCATION", "KODE_GARDU", "ASSETNUM", "NAMA", "PENYULANG", "UP3", "ULP", "KAPASITAS_KVA", "JUMLAH_PELANGGAN"])
    for r in db.all("SELECT gardu_id, code, props->>'ASSETNUM' AS assetnum, name, feeder, up3, ulp, capacity_kva FROM assets "
                    "WHERE active AND kind='trafo' ORDER BY up3, feeder, name"):
        w.writerow([r["gardu_id"] or "", r["code"] or "", r["assetnum"] or "", r["name"], r["feeder"] or "", r["up3"] or "",
                    r["ulp"] or "", "" if r["capacity_kva"] is None else int(r["capacity_kva"]), ""])
    return out.getvalue()
