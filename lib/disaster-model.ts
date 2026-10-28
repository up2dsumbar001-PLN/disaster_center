import type { Feature, FeatureCollection, Geometry, Position } from "geojson";
import { booleanIntersects, buffer, circle } from "@turf/turf";

export type Asset = Feature<Geometry, Record<string, unknown>>;
export type Warning = {
  id: string; title: string; description: string; instruction: string; severity: string;
  certainty: string; urgency: string; sent: string; effective: string; expires: string;
  status: string; msgType: string; references: string[]; areas: Feature[]; areasText: string[];
};
export const EARTH_URL = "https://earth.google.com/web/@-0.70277044,100.06366709,23.31608844a,669450.31051889d,35y,0h,0t,0r/data=CgRCAggBMikKJwolCiExQ2RXSW1lZEdDN25YRjRucmw3RFpReXJPbncxZk1VWTkgAToDCgEwQgIIAEoICOeupKoGEAE?authuser=0";
export const elements = (el: Document | Element, name: string): Element[] => Array.from(el.getElementsByTagNameNS("*", name));
export const txt = (el: Document | Element, name: string) => elements(el, name)[0]?.textContent?.trim() || "";
export function xml(text: string) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (elements(doc, "parsererror").length) throw new Error("XML tidak valid.");
  return doc;
}
export function parseCap(text: string): Warning {
  const doc = xml(text);
  const infos = elements(doc, "info");
  const info = infos.find(i => /^id/i.test(txt(i, "language"))) || infos[0];
  const id = txt(doc, "identifier");
  if (!id || !txt(doc, "sent")) throw new Error("Identitas CAP tidak lengkap.");
  const result: Warning = { id, title: info ? txt(info, "headline") || txt(info, "event") : "Pembaruan peringatan", description: info ? txt(info, "description") : "", instruction: info ? txt(info, "instruction") : "", severity: info ? txt(info, "severity") : "Unknown", certainty: info ? txt(info, "certainty") : "Unknown", urgency: info ? txt(info, "urgency") : "Unknown", sent: txt(doc, "sent"), effective: info ? txt(info, "effective") || txt(doc, "sent") : "", expires: info ? txt(info, "expires") : "", status: txt(doc, "status"), msgType: txt(doc, "msgType"), references: txt(doc, "references").split(/\s+/).filter(Boolean).map(s => s.split(",")[1]).filter(Boolean), areas: [], areasText: [] };
  if (!info) return result;
  for (const area of elements(info, "area")) {
    result.areasText.push(txt(area, "areaDesc"));
    for (const p of elements(area, "polygon")) {
      const ring: Position[] = (p.textContent || "").trim().split(/\s+/).filter(Boolean).map(pair => {
        const [lat, lon] = pair.split(",").map(Number);
        if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) throw new Error("Koordinat CAP tidak valid.");
        return [lon, lat];
      });
      if (ring.length < 3) throw new Error("Polygon CAP tidak lengkap.");
      if (String(ring[0]) !== String(ring[ring.length - 1])) ring.push([...ring[0]]);
      result.areas.push({ type: "Feature", properties: { warningId: id }, geometry: { type: "Polygon", coordinates: [ring] } });
    }
    for (const c of elements(area, "circle")) {
      const [center, radiusText] = (c.textContent || "").trim().split(/\s+/);
      const [lat, lon] = (center || "").split(",").map(Number); const radius = Number(radiusText);
      if (![lat, lon, radius].every(Number.isFinite) || Math.abs(lat) > 90 || Math.abs(lon) > 180 || radius <= 0) throw new Error("Lingkaran CAP tidak valid.");
      result.areas.push(circle([lon, lat], radius, { units: "kilometers", steps: 64 }));
    }
  }
  return result;
}
export function activeWarnings(all: Warning[], at = Date.now()) {
  const suppressed = new Set(all.filter(w => w.status === "Actual" && ["Update", "Cancel"].includes(w.msgType) && Date.parse(w.sent) <= at).flatMap(w => w.references));
  const unique = new Map(all.map(w => [w.id, w]));
  return [...unique.values()].filter(w => w.status === "Actual" && ["Alert", "Update"].includes(w.msgType) && !suppressed.has(w.id) && Date.parse(w.sent) <= at && Date.parse(w.effective) <= at && Date.parse(w.expires) > at);
}
export function assetName(a: Asset) { return String(a.properties?.name || a.properties?.nama || a.properties?.asset_id || a.id || "Aset tanpa nama"); }
export function assetType(a: Asset) {
  const raw = String(a.properties?.asset_type || a.properties?.jenis || "").toLowerCase();
  const name = assetName(a).toLowerCase();
  if (/gardu|trafo|substation/.test(raw + " " + name)) return "Gardu";
  if (/tiang|pole/.test(raw + " " + name)) return "Tiang";
  if (/LineString/.test(a.geometry.type)) return "Jaringan";
  return "Aset lain";
}
export function exposure(assets: Asset[], warnings: Warning[], corridorMeters: number) {
  return assets.map(a => {
    let g: Feature = a;
    if (corridorMeters > 0 && /LineString/.test(a.geometry.type)) {
      const b = buffer(a, corridorMeters / 1000, { units: "kilometers" });
      if (!b) throw new Error(`Koridor aset ${assetName(a)} gagal dihitung.`);
      g = b;
    }
    return { asset: a, warnings: warnings.filter(w => w.areas.some(p => booleanIntersects(g, p))) };
  });
}
function validateGeometry(g: Geometry): void {
  if (!g || typeof g !== "object") throw new Error("Geometri aset kosong.");
  if (g.type === "GeometryCollection") { if (!g.geometries.length) throw new Error("Geometri kosong."); g.geometries.forEach(validateGeometry); return; }
  const levels = { Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3 };
  if (!(g.type in levels)) throw new Error("Jenis geometri belum didukung.");
  const check = (v: unknown, depth: number): void => {
    if (!Array.isArray(v) || v.length === 0) throw new Error("Koordinat kosong.");
    if (depth === 0) {
      if (v.length < 2 || !Number.isFinite(v[0]) || !Number.isFinite(v[1]) || Math.abs(v[0]) > 180 || Math.abs(v[1]) > 90) throw new Error("Koordinat aset harus WGS84 [bujur, lintang].");
    } else v.forEach(x => check(x, depth - 1));
  };
  check(g.coordinates, levels[g.type]);
  if (g.type === "LineString" && g.coordinates.length < 2) throw new Error("Ruas jaringan membutuhkan minimal dua koordinat.");
  const rings = g.type === "Polygon" ? g.coordinates : g.type === "MultiPolygon" ? g.coordinates.flat() : [];
  if (rings.some(r => r.length < 4 || String(r[0]) !== String(r[r.length - 1]))) throw new Error("Cincin polygon harus tertutup dengan minimal empat koordinat.");
}
export function validateAssets(value: unknown): Asset[] {
  const fc = value as FeatureCollection;
  if (fc?.type !== "FeatureCollection" || !Array.isArray(fc.features)) throw new Error("Gunakan GeoJSON FeatureCollection.");
  if (!fc.features.length) throw new Error("Tidak ada aset dalam berkas.");
  if (fc.features.length > 30_000) throw new Error("Prototipe membatasi 30.000 fitur per impor. Pecah data per UP3 untuk volume lebih besar.");
  return fc.features.map((f, i) => {
    if (f.type !== "Feature") throw new Error(`Fitur ${i + 1} tidak valid.`);
    validateGeometry(f.geometry);
    return { ...f, id: i + 1, properties: { ...(f.properties || {}), source_id: f.id ?? null, _dccId: `asset-${i}` } };
  });
}
export const demoAssets: Asset[] = [
  ...[[100.355, -0.915], [100.365, -0.925], [100.38, -0.94], [100.4, -0.96], [100.425, -0.978], [100.45, -1.01], [100.32, -0.84], [100.39, -0.86]].map((coordinates, i) => ({ type: "Feature" as const, geometry: { type: "Point" as const, coordinates }, properties: { name: `SIMULASI ${i < 4 ? "Gardu" : "Tiang"} ${String(i + 1).padStart(2, "0")}`, _dccId: `demo-${i}`, feeder: "Penyulang simulasi" } })),
  { type: "Feature", geometry: { type: "LineString", coordinates: [[100.32,-0.84],[100.355,-0.915],[100.38,-0.94],[100.425,-0.978],[100.45,-1.01]] }, properties: { name: "SIMULASI Penyulang 20 kV", _dccId: "demo-line" } },
];
export function demoWarning(): Warning { const now = Date.now(); return { id: "SIMULASI-CAP-001", title: "SIMULASI · Hujan lebat dan petir", description: "Contoh area peringatan di sekitar Padang untuk menguji tampilan dan irisan aset. Bukan data BMKG dan bukan kondisi cuaca aktual.", instruction: "Latihan: verifikasi kondisi lapangan dan kesiapan petugas sesuai SOP setempat.", severity: "Severe", certainty: "Likely", urgency: "Expected", status: "Actual", msgType: "Alert", references: [], sent: new Date(now - 60_000).toISOString(), effective: new Date(now - 60_000).toISOString(), expires: new Date(now + 7200_000).toISOString(), areasText: ["Area simulasi Padang"], areas: [{ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[100.34,-0.90],[100.41,-0.90],[100.44,-0.98],[100.36,-0.99],[100.34,-0.90]]] } }] }; }
