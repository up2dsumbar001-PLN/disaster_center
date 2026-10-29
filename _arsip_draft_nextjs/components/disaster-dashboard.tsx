"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDownToLine, ArrowUpRight, Bell, Check, ChevronRight, CloudLightning, CloudRain, Database, ExternalLink, FileUp, Focus, Globe2, Layers3, MapPin, RadioTower, RefreshCw, Search, Settings2, ShieldAlert, Volume2, VolumeX, X, Zap } from "lucide-react";
import type { Map as LeafletMap, LayerGroup } from "leaflet";
import type { FeatureCollection } from "geojson";
import { kml } from "@tmcw/togeojson";
import { unzipSync, strFromU8 } from "fflate";
import { activeWarnings, assetName, assetType, demoAssets, demoWarning, EARTH_URL, elements, exposure, parseCap, txt, validateAssets, xml, type Asset, type Warning } from "@/lib/disaster-model";

type Health = { state: "loading" | "ok" | "error"; checked: string; error?: string; sourceTime?: string; partial?: boolean };
type Quake = { DateTime: string; Coordinates: string; Magnitude: string; Kedalaman: string; Wilayah: string; Potensi: string; Dirasakan: string };
type Forecast = { lokasi?: { desa?: string; kecamatan?: string }; data?: { cuaca: { utc_datetime: string; weather_desc: string; t: number; hu: number; ws: number; wd: string; analysis_date?: string }[][] }[] };
const waiting: Health = { state: "loading", checked: "" };
const time = (s?: string, full = false) => s && Number.isFinite(Date.parse(s)) ? new Intl.DateTimeFormat("id-ID", { timeZone: "Asia/Jakarta", ...(full ? { day: "2-digit", month: "short" } : {}), hour: "2-digit", minute: "2-digit" }).format(new Date(s)) + " WIB" : "—";
const statusLabel = (h: Health) => h.state === "loading" ? "Menghubungkan" : h.state === "error" ? "Koneksi gagal" : h.partial ? "Data sebagian" : "Terhubung";
async function getData(product: string, extra = "") {
  const r = await fetch(`/api/bmkg?product=${product}${extra}`, { signal: AbortSignal.timeout(22_000), cache: "no-store" });
  if (!r.ok) { let error = `HTTP ${r.status}`; try { error = ((await r.json()) as {error?: string}).error || error; } catch {} throw new Error(error); }
  return r;
}
function escapeHtml(s: unknown) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!)); }
const download = (name: string, content: unknown) => { const url = URL.createObjectURL(new Blob([JSON.stringify(content, null, 2)], { type: "application/json" })); const a = document.createElement("a"); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); };

export default function Dashboard() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [allWarnings, setWarnings] = useState<Warning[]>([]);
  const [nowcastHealth, setNowcastHealth] = useState<Health>(waiting);
  const [quakeHealth, setQuakeHealth] = useState<Health>(waiting);
  const [quake, setQuake] = useState<Quake | null>(null);
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState(false);
  const [sampleWarning, setSampleWarning] = useState<Warning | null>(null);
  const [tick, setTick] = useState(0);
  const [clock, setClock] = useState("");
  const [fileName, setFileName] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("Semua");
  const [selected, setSelected] = useState<Asset | null>(null);
  const [corridor, setCorridor] = useState(100);
  const [settings, setSettings] = useState(false);
  const [layerAssets, setLayerAssets] = useState(true);
  const [layerWarnings, setLayerWarnings] = useState(true);
  const [layerQuake, setLayerQuake] = useState(false);
  const [sound, setSound] = useState(false);
  const [acked, setAcked] = useState<Set<string>>(new Set());
  const [adm4, setAdm4] = useState("");
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [forecastHealth, setForecastHealth] = useState<Health | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [mapError, setMapError] = useState("");
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<LeafletMap | null>(null);
  const groups = useRef<{ assets: LayerGroup; warnings: LayerGroup; quake: LayerGroup } | null>(null);
  const leaflet = useRef<typeof import("leaflet") | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const refreshLock = useRef(false);
  const audio = useRef<AudioContext | null>(null);
  const seenAlerts = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    if (refreshLock.current) return;
    refreshLock.current = true; setBusy(true);
    try {
      await Promise.allSettled([
        (async () => {
          try {
            const r = await getData("nowcast"); const feed = xml(await r.text());
            if (!elements(feed, "rss").length && !elements(feed, "feed").length) throw new Error("Feed peringatan tidak valid.");
            const items = elements(feed, "item").filter(i => /sumat(?:e|ra)?(?:ra)?\s*barat|sumbar/i.test(txt(i, "title") + " " + txt(i, "description")));
            const links = [...new Set(items.map(i => txt(i, "link")))];
            const results = await Promise.allSettled(links.slice(0, 8).map(async link => {
              const u = new URL(link, "https://www.bmkg.go.id");
              if (u.origin !== "https://www.bmkg.go.id" || !u.pathname.startsWith("/alerts/nowcast/id/")) throw new Error("Tautan CAP di luar jalur resmi.");
              const f = u.pathname.split("/").pop()!;
              const detail = await getData("cap", `&file=${encodeURIComponent(f)}`);
              return parseCap(await detail.text());
            }));
            const good = results.flatMap(v => v.status === "fulfilled" ? [v.value] : []);
            const failures = results.filter(v => v.status === "rejected");
            if (links.length && !good.length) throw new Error("Feed tersedia, tetapi detail CAP Sumatera Barat gagal diambil.");
            // Replace snapshots. Never keep a vanished warning as an active one.
            setWarnings(good);
            const missingGeometry = activeWarnings(good).some(w => !w.areas.length);
            setNowcastHealth({ state: "ok", checked: r.headers.get("X-Fetched-At") || new Date().toISOString(), sourceTime: txt(feed, "lastBuildDate"), partial: failures.length > 0 || links.length > 8 || missingGeometry, error: failures.length ? `${failures.length} detail CAP belum tersedia.` : missingGeometry ? "Sebagian peringatan tidak memiliki geometri." : undefined });
          } catch (e) { setNowcastHealth(h => ({ ...h, state: "error", error: e instanceof Error ? e.message : "Koneksi gagal" })); }
        })(),
        (async () => {
          try { const r = await getData("quake"); const body = await r.json() as {Infogempa?: {gempa?: Quake}}; const g = body?.Infogempa?.gempa; if (!g?.DateTime || !g?.Coordinates) throw new Error("Format gempa tidak sesuai."); setQuake(g); setQuakeHealth({ state: "ok", checked: r.headers.get("X-Fetched-At") || new Date().toISOString(), sourceTime: g.DateTime }); }
          catch (e) { setQuakeHealth(h => ({ ...h, state: "error", error: e instanceof Error ? e.message : "Koneksi gagal" })); }
        })(),
      ]);
    } finally { refreshLock.current = false; setBusy(false); }
  }, []);
  useEffect(() => { void refresh(); const id = setInterval(() => void refresh(), 60_000); return () => clearInterval(id); }, [refresh]);
  useEffect(() => { const update = () => { setClock(new Intl.DateTimeFormat("id-ID", { timeZone: "Asia/Jakarta", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date())); setTick(Date.now()); }; update(); const id = setInterval(update, 1000); return () => clearInterval(id); }, []);
  useEffect(() => {
    let cancelled = false;
    import("leaflet").then(L => {
      if (cancelled || !container.current || map.current) return;
      leaflet.current = L;
      const m = L.map(container.current, { zoomControl: false, preferCanvas: true }).setView([-0.70277044, 100.06366709], 7);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>', maxZoom: 19, className: "dcc-basemap" }).on("tileerror", () => setMapError("Sebagian tile peta gagal dimuat. Periksa koneksi internet.")).addTo(m);
      L.control.zoom({ position: "bottomright" }).addTo(m); L.control.scale({ imperial: false, position: "bottomleft" }).addTo(m);
      groups.current = { assets: L.layerGroup().addTo(m), warnings: L.layerGroup().addTo(m), quake: L.layerGroup().addTo(m) }; map.current = m; setMapReady(true);
    }).catch(() => setMapError("Peta gagal dimuat. Muat ulang halaman."));
    return () => { cancelled = true; map.current?.remove(); map.current = null; groups.current = null; };
  }, []);
  const shownAssets = demo ? demoAssets : assets;
  const warnings = useMemo(() => activeWarnings(demo && sampleWarning ? [sampleWarning] : allWarnings, tick), [demo, sampleWarning, allWarnings, Math.floor(tick / 15000)]);
  const analysis = useMemo(() => { try { return { rows: exposure(shownAssets, warnings, corridor), error: "" }; } catch (e) { return { rows: [], error: e instanceof Error ? e.message : "Analisis spasial gagal" }; } }, [shownAssets, warnings, corridor]);
  const exposed = useMemo(() => analysis.rows.filter(r => r.warnings.length), [analysis.rows]);
  const fresh = demo || (nowcastHealth.state === "ok" && !nowcastHealth.partial && tick - Date.parse(nowcastHealth.checked) <= 180_000);
  const filtered = useMemo(() => shownAssets.filter(a => (filter === "Semua" || assetType(a) === filter || (filter === "Terpapar" && exposed.some(r => r.asset === a))) && (assetName(a) + " " + String(a.properties?.feeder || a.properties?.penyulang || "")).toLowerCase().includes(query.toLowerCase())), [shownAssets, filter, exposed, query]);
  useEffect(() => {
    const L = leaflet.current, gs = groups.current; if (!mapReady || !L || !gs) return;
    gs.assets.clearLayers(); gs.warnings.clearLayers(); gs.quake.clearLayers();
    if (layerWarnings) warnings.forEach(w => w.areas.forEach(p => { L.geoJSON(p, { style: { color: "#f9b456", weight: 1.5, fillColor: "#f5a947", fillOpacity: .13, dashArray: "6 5" } }).bindPopup(`<b>${escapeHtml(w.title)}</b><p>Berlaku hingga ${escapeHtml(time(w.expires))}</p><p>Area peringatan resmi, bukan radar hujan.</p>`).addTo(gs.warnings); }));
    if (layerAssets) filtered.forEach(a => {
      const hit = exposed.some(r => r.asset === a); const color = hit ? "#ff775f" : assetType(a) === "Gardu" ? "#6ce4dc" : "#a7c2ed";
      L.geoJSON(a, { style: { color, weight: a === selected ? 5 : 2.5, opacity: .9 }, pointToLayer: (_, latlng) => L.circleMarker(latlng, { radius: a === selected ? 8 : assetType(a) === "Gardu" ? 5.5 : 3.5, color, fillColor: color, fillOpacity: .9, weight: 2 }), onEachFeature: (_, layer) => { layer.bindTooltip(escapeHtml(assetName(a)), { direction: "top" }); layer.on("click", () => setSelected(a)); } }).addTo(gs.assets);
    });
    if (layerQuake && !demo && quake && quakeHealth.state === "ok") { const [lat, lon] = quake.Coordinates.split(",").map(Number); if (Number.isFinite(lat) && Number.isFinite(lon)) L.circleMarker([lat, lon], { radius: 10, color: "#bfa0ff", fillOpacity: .3 }).bindPopup(`<b>Gempa M ${escapeHtml(quake.Magnitude)}</b><p>${escapeHtml(quake.Wilayah)}</p><p>${escapeHtml(time(quake.DateTime, true))}</p>`).addTo(gs.quake); }
  }, [mapReady, filtered, warnings, layerAssets, layerWarnings, layerQuake, selected, demo, quake, quakeHealth.state]);
  const focusAssets = () => { const L = leaflet.current; if (L && map.current && shownAssets.length) { const b = L.geoJSON({ type: "FeatureCollection", features: shownAssets } as FeatureCollection).getBounds(); if (b.isValid()) map.current.fitBounds(b, { padding: [45,45], maxZoom: 15 }); } else map.current?.setView([-0.70277044,100.06366709],7); };
  const pick = (a: Asset) => { setSelected(a); const L = leaflet.current; if (L && map.current) { const b = L.geoJSON(a).getBounds(); if (b.isValid()) map.current.fitBounds(b, { padding: [90,90], maxZoom: 15 }); } };
  useEffect(() => { if (demo && mapReady) map.current?.setView([-0.935,100.385],12); else if (mapReady) map.current?.setView([-0.70277044,100.06366709],7); }, [demo, mapReady]);
  useEffect(() => { const ids = exposed.flatMap(r => r.warnings.map(w => `${demo}:${w.id}`)); const novel = ids.some(id => !seenAlerts.current.has(id)); ids.forEach(id => seenAlerts.current.add(id)); if (!sound || !novel || !audio.current || !fresh) return; const ctx = audio.current; const oscillator = ctx.createOscillator(); const gain = ctx.createGain(); oscillator.connect(gain); gain.connect(ctx.destination); oscillator.frequency.value = 740; gain.gain.value = .08; oscillator.start(); oscillator.stop(ctx.currentTime + .3); }, [exposed, sound, demo, fresh]);

  async function importFile(file: File) {
    try {
      if (file.size > 20_000_000) throw new Error("Batas berkas 20 MB.");
      let data: unknown; let content = "";
      if (/\.kmz$/i.test(file.name)) {
        let expandedBytes = 0;
        const archive = unzipSync(new Uint8Array(await file.arrayBuffer()), { filter: f => { if (!/\.kml$/i.test(f.name)) return false; expandedBytes += f.originalSize; if (expandedBytes > 40_000_000) throw new Error("Total KML dalam KMZ melebihi 40 MB."); return true; } });
        const files = Object.keys(archive).filter(n => /\.kml$/i.test(n));
        const main = files.find(n => /(^|\/)doc\.kml$/i.test(n)) || (files.length === 1 ? files[0] : "");
        if (!main) throw new Error("KMZ harus berisi doc.kml atau satu berkas KML utama.");
        content = strFromU8(archive[main]);
      } else content = await file.text();
      if (/\.(?:kml|kmz)$/i.test(file.name)) { const doc = xml(content); if (elements(doc, "NetworkLink").length) throw new Error("KML berisi NetworkLink. Ekspor geometri aset secara langsung agar seluruh aset dapat diperiksa."); data = kml(doc); }
      else data = JSON.parse(content);
      const next = validateAssets(data); setAssets(next); setFileName(file.name); setSelected(null); setDemo(false); setNotice(`${next.length.toLocaleString("id-ID")} fitur berhasil diimpor. Data berada di sesi browser ini.`);
      const L = leaflet.current; if (L && map.current) setTimeout(() => { const b = L.geoJSON({ type: "FeatureCollection", features: next } as FeatureCollection).getBounds(); if (b.isValid()) map.current?.fitBounds(b, { padding: [45,45], maxZoom: 14 }); }, 200);
    } catch (e) { setNotice(e instanceof Error ? e.message : "Impor gagal."); }
  }
  async function loadForecast() {
    if (!/^13\.\d{2}\.\d{2}\.\d{4}$/.test(adm4)) { setNotice("Masukkan kode desa/kelurahan Sumatera Barat yang valid (13.xx.xx.xxxx). Kode harus diverifikasi terhadap wilayah aset."); return; }
    setForecastHealth(waiting); setForecast(null);
    try { const r = await getData("forecast", `&adm4=${encodeURIComponent(adm4)}`); const body: Forecast = await r.json(); if (!body.data?.[0]?.cuaca) throw new Error("Data prakiraan tidak tersedia untuk kode ini."); setForecast(body); setForecastHealth({ state: "ok", checked: r.headers.get("X-Fetched-At") || new Date().toISOString() }); }
    catch(e) { setForecastHealth({ state: "error", checked: "", error: e instanceof Error ? e.message : "Prakiraan gagal" }); }
  }
  const forecastRows = forecast?.data?.[0]?.cuaca.flat().filter(x => Date.parse(x.utc_datetime.replace(" ", "T") + "Z") >= tick - 10800_000).slice(0,8) || [];
  const toggleSound = async () => { if (!sound) { audio.current ||= new AudioContext(); await audio.current.resume(); } setSound(!sound); };

  return <main className="dcc-app">
    <header className="topbar">
      <div className="brand"><span className="brand-icon"><Zap size={23} strokeWidth={1.7}/></span><div><h1>DISASTER<span>CENTER</span></h1><p>DISTRIBUTION CONTROL CENTER · 20 kV</p></div></div>
      <div className="region"><span className="pulse-dot"/>SUMATERA BARAT<span className="region-sub">Pemantauan wilayah</span></div>
      <div className="top-actions"><div className="clock">{clock || "--.--.--"}<small>WIB · UTC+7</small></div><button title="Pengaturan dan konektor" aria-label="Pengaturan dan konektor" onClick={() => setSettings(true)}><Settings2 size={18}/></button><button className="operator" onClick={() => setSettings(true)} title="Informasi prototipe">DC</button></div>
    </header>
    <nav className="navline"><div className="nav-active"><Globe2 size={15}/> Situational awareness</div><span><ShieldAlert size={14}/> Pendukung keputusan operator</span><div className="nav-right"><span className={`connection ${demo ? "demo" : fresh ? "good" : "warn"}`}>{demo ? "MODE SIMULASI" : fresh ? "BMKG TERHUBUNG" : "DATA BELUM TERVERIFIKASI"}</span><button onClick={() => { setDemo(!demo); setSampleWarning(demoWarning()); setSelected(null); setAcked(new Set()); }}>{demo ? "Kembali ke data BMKG" : "Coba simulasi"}</button></div></nav>
    <section className="workspace">
      <aside className="left-panel panel">
        <div className="section-label">BASELINE JARINGAN <span>01</span></div>
        <h2>Aset distribusi</h2><p className="muted">Gardu, tiang & jalur penyulang</p>
        <input ref={fileInput} hidden type="file" accept=".kml,.kmz,.geojson,.json" onChange={e => { const f = e.target.files?.[0]; if(f) void importFile(f); e.target.value = ""; }}/>
        <button className="import-button" onClick={() => fileInput.current?.click()}><FileUp size={17}/><span>Impor aset jaringan<small>KML · KMZ · GeoJSON</small></span><ChevronRight size={16}/></button>
        <a className="earth-link" href={EARTH_URL} target="_blank" rel="noreferrer"><Globe2 size={15}/> Buka baseline Google Earth <ArrowUpRight size={14}/></a>
        <div className="file-state"><Database size={13}/><span>{demo ? "Dataset latihan · 9 fitur" : fileName || "Belum ada aset diimpor"}</span></div>
        <div className="divider"/>
        <label className="searchbox"><Search size={15}/><input aria-label="Cari aset atau penyulang" placeholder="Cari aset / penyulang…" value={query} onChange={e=>setQuery(e.target.value)}/></label>
        <div className="filters">{["Semua","Gardu","Tiang","Jaringan","Terpapar"].map(f=><button key={f} className={filter===f?"active":""} onClick={()=>setFilter(f)}>{f}</button>)}</div>
        <div className="asset-list">{filtered.length ? filtered.slice(0,150).map(a=><button className={`asset-row ${selected===a?"selected":""}`} key={String(a.properties?._dccId)} onClick={()=>pick(a)}><span className={`asset-symbol ${exposed.some(r=>r.asset===a)?"exposed":""}`}>{assetType(a)==="Gardu"?<Zap size={14}/>:assetType(a)==="Jaringan"?<Activity size={14}/>:<MapPin size={14}/>}</span><span><strong>{assetName(a)}</strong><small>{assetType(a)} · {String(a.properties?.feeder || a.properties?.penyulang || "Penyulang belum dipetakan")}</small></span><ChevronRight size={13}/></button>):<div className="empty-assets"><RadioTower size={33} strokeWidth={1}/><h3>{shownAssets.length ? "Aset tidak ditemukan" : "Hubungkan baseline Anda"}</h3><p>{shownAssets.length ? "Ubah kata pencarian atau filter." : "Ekspor proyek Google Earth sebagai KML, lalu impor di sini untuk mulai memetakan paparan jaringan."}</p></div>}{filtered.length>150&&<p className="muted">150 dari {filtered.length} fitur. Gunakan pencarian.</p>}</div>
        <div className="layer-section"><div className="section-label">LAPISAN PETA <Layers3 size={14}/></div><label><span><i className="dot cyan"/>Aset jaringan</span><input type="checkbox" checked={layerAssets} onChange={e=>setLayerAssets(e.target.checked)}/></label><label><span><i className="dot amber"/>Area peringatan CAP</span><input type="checkbox" checked={layerWarnings} onChange={e=>setLayerWarnings(e.target.checked)}/></label><label><span><i className="dot purple"/>Gempa terakhir · nasional</span><input type="checkbox" checked={layerQuake} onChange={e=>setLayerQuake(e.target.checked)}/></label><div className="unavailable"><CloudRain size={13}/> Radar & lintasan hujan <span>Belum terhubung</span></div></div>
      </aside>
      <section className="center-panel">
        <div className="metrics"><div className="metric"><span><Database size={14}/> ASET DIMUAT</span><strong>{shownAssets.length.toLocaleString("id-ID")}<small>fitur</small></strong><p>{demo?"Data simulasi":assets.length?"Baseline diimpor":"Menunggu baseline jaringan"}</p></div><div className="metric danger"><span><ShieldAlert size={14}/> PAPARAN PERINGATAN</span><strong>{analysis.error || !fresh || !shownAssets.length ? "—" : exposed.length}<small>aset</small></strong><p>{!shownAssets.length?"Menunggu baseline jaringan":!fresh?"Status belum dapat dipastikan":analysis.error?"Analisis spasial gagal":"Irisan aset / koridor dengan CAP"}</p></div><div className="metric"><span><CloudLightning size={14}/> PERINGATAN AKTIF</span><strong>{fresh?warnings.length:"—"}<small>CAP</small></strong><p>{demo?"Skenario latihan":fresh?"Wilayah Sumatera Barat":"Menunggu data BMKG valid"}</p></div><div className="metric"><span><Activity size={14}/> RADAR NOWCAST</span><strong className="pending-value">Belum aktif</strong><p>Waktu tiba hujan belum tersedia</p></div></div>
        <div className="map-shell">
          <div className="map-heading"><div><span className="eyebrow">PETA OPERASI</span><h2>Sumatera Barat <span>/ 20 kV</span></h2></div><button onClick={focusAssets}><Focus size={15}/> Fokus jaringan</button></div>
          <div ref={container} className="operation-map" aria-label="Peta aset jaringan dan peringatan cuaca Sumatera Barat"/>
          <div className={`map-banner ${demo?"simulation":""}`}><span className="pulse-dot"/>{demo?"SIMULASI — aset dan cuaca fiktif untuk latihan": "Peringatan CAP BMKG · bukan citra radar hujan"}</div>
          {mapError&&<div className="map-error">{mapError}</div>}
          {!demo&&!assets.length&&<div className="map-onboard"><span className="onboard-number">01 / MULAI PEMANTAUAN</span><h3>Cuaca bertemu<br/>jaringan Anda.</h3><p>Impor aset dari Google Earth untuk melihat gardu dan ruas jaringan yang berada di area peringatan BMKG.</p><button onClick={()=>fileInput.current?.click()}><FileUp size={15}/> Impor baseline jaringan <ArrowUpRight size={15}/></button><small>Basemap awal: OpenStreetMap 2D.<br/>Tampilan Google berlisensi tersedia pada tahap integrasi.</small></div>}
          <div className="map-legend"><span><i className="dot cyan"/>Aset</span><span><i className="dot red"/>Terpapar CAP</span><span><i className="legend-area"/>Area peringatan</span></div>
          {selected&&<div className="asset-detail"><button className="close" aria-label="Tutup detail aset" onClick={()=>setSelected(null)}><X size={15}/></button><span className="eyebrow">DETAIL ASET · {assetType(selected)}</span><h3>{assetName(selected)}</h3><p>{!fresh?"Paparan belum dapat dipastikan karena data cuaca tidak valid.":exposed.some(r=>r.asset===selected)?"Beririsan dengan area peringatan. Verifikasi kondisi dan siapkan tindak lanjut sesuai SOP.":"Tidak beririsan dengan CAP yang dimuat. Ini bukan jaminan bebas risiko."}</p>{Object.entries(selected.properties||{}).filter(([k])=>!k.startsWith("_")&&!['description','styleUrl','styleHash'].includes(k)).slice(0,7).map(([k,v])=><div className="property" key={k}><span>{k}</span><b>{String(v).slice(0,120)}</b></div>)}</div>}
        </div>
        <div className="bottom-grid"><div className="source-panel panel"><div className="section-label">KESEHATAN DATA <Activity size={14}/></div><div className="source-row"><span className={`source-dot ${nowcastHealth.state}`}/><div><strong>BMKG · Peringatan dini</strong><small>{nowcastHealth.error || `Pemeriksaan ${time(nowcastHealth.checked)}`}</small></div><span className="source-state">{statusLabel(nowcastHealth)}</span></div><div className="source-row"><span className={`source-dot ${quakeHealth.state}`}/><div><strong>BMKG · Gempabumi</strong><small>{quakeHealth.error || `Pemeriksaan ${time(quakeHealth.checked)}`}</small></div><span className="source-state">{statusLabel(quakeHealth)}</span></div><button className="text-button" disabled={busy} onClick={()=>void refresh()}><RefreshCw size={13} className={busy?"spin":""}/>{busy?"Memeriksa sumber…":"Periksa kembali"}<span>Pembaruan otomatis 60 dtk</span></button></div><div className="quake-panel panel"><div className="section-label">GEMPA TERAKHIR · INDONESIA <Activity size={14}/></div>{quake?<><div className="quake-reading"><strong>{quake.Magnitude}</strong><div><b>Magnitudo</b><small>{time(quake.DateTime,true)}{quakeHealth.state!=="ok"?" · salinan terakhir":""}</small></div></div><p>{quake.Wilayah}</p><small className="muted">Kedalaman {quake.Kedalaman} · {quake.Potensi}</small></>:<div className="no-quake"><Activity size={22}/><p>Menunggu data kejadian BMKG</p><small>Gempa nasional tidak otomatis berarti aset terdampak.</small></div>}</div></div>
      </section>
      <aside className="right-panel panel"><div className="alert-heading"><div><span className="section-label">PUSAT PERINGATAN</span><h2>Weather watch <span>{fresh?warnings.length:"—"}</span></h2></div><button title={sound?"Matikan suara":"Aktifkan suara"} aria-label={sound?"Matikan suara":"Aktifkan suara"} onClick={()=>void toggleSound()}>{sound?<Volume2 size={18}/>:<VolumeX size={18}/>}</button></div>
        <div className={`confidence-banner ${fresh?"valid":""}`}><ShieldAlert size={16}/><p>{demo?"Skenario simulasi sedang ditampilkan.":fresh?"Peringatan resmi BMKG tersedia.":"Kondisi cuaca belum dapat dipastikan."}<small>{demo?"Jangan gunakan untuk keputusan operasi.":fresh?"Paparan aset dihitung dari area CAP.":"Data tidak tersedia bukan berarti aman."}</small></p></div>
        <div className="alert-list">{warnings.length?warnings.map(w=><article className="weather-alert" key={w.id}><div className="alert-kicker"><span><i className="dot amber"/>{demo?"SIMULASI":w.severity.toUpperCase()}</span><small>{time(w.sent)}</small></div><h3>{w.title}</h3><p>{w.description}</p><div className="alert-times"><span>BERLAKU HINGGA</span><b>{time(w.expires,true)}</b></div><div className="impact-box"><RadioTower size={15}/><span>{analysis.error?"Analisis gagal":exposed.filter(r=>r.warnings.some(x=>x.id===w.id)).length} aset beririsan{!fresh?" · data terakhir":""}</span><button onClick={()=>{setFilter("Terpapar");focusAssets();}}>Lihat <ChevronRight size={13}/></button></div>{!w.areas.length&&<p className="amber-text">CAP tidak memuat geometri. Paparan aset tidak dapat dihitung.</p>}{w.instruction&&<p className="instruction">{w.instruction}</p>}<button className={`ack-button ${acked.has(w.id)?"acked":""}`} onClick={()=>setAcked(s=>new Set([...s,w.id]))}>{acked.has(w.id)?<><Check size={14}/> Ditinjau operator · sesi ini</>:<><Check size={14}/> Tandai sudah ditinjau</>}</button></article>):<div className="empty-alert"><CloudLightning size={38} strokeWidth={1}/><h3>{fresh?"Tidak ada CAP aktif":"Menunggu peringatan valid"}</h3><p>{fresh?"Tidak ditemukan peringatan aktif pada feed Sumatera Barat yang berhasil dimuat. Tetap pantau perubahan kondisi.":"Sistem akan menampilkan peringatan resmi dan aset yang beririsan setelah konektor BMKG terhubung."}</p></div>}</div>
        {analysis.error&&<p className="error-text">{analysis.error}</p>}
        <div className="forecast-panel"><div className="section-label">PRAKIRAAN LOKASI <CloudRain size={14}/></div><p className="muted">Kode desa/kelurahan Kemendagri tingkat IV.</p><div className="forecast-input"><input value={adm4} onChange={e=>setAdm4(e.target.value)} placeholder="13.xx.xx.xxxx" aria-label="Kode wilayah desa atau kelurahan"/><button onClick={()=>void loadForecast()} disabled={demo||forecastHealth?.state==="loading"}>Muat</button></div>{demo&&<small className="amber-text">Prakiraan aktual dinonaktifkan dalam simulasi.</small>}{forecastHealth?.state==="error"&&<p className="error-text">{forecastHealth.error}</p>}{forecast&&!demo&&<><h3>{forecast.lokasi?.desa || "Prakiraan BMKG"}</h3><small className="muted">Interval 3 jam · bukan observasi langsung</small>{forecastRows.map((r,i)=><div className="forecast-row" key={i}><b>{time(r.utc_datetime.replace(" ","T")+"Z",true)}</b><span>{r.weather_desc}</span><strong>{r.t}°</strong><small>Angin {r.ws} km/jam · {r.wd}</small></div>)}</>}</div>
        <div className="ops-note"><span>CATATAN OPERASI</span><p>Peringatan membantu penilaian operator. Tindak lanjut mengikuti SOP DCC dan verifikasi lapangan.</p><small>SCADA belum terhubung · tanpa perintah switching</small></div>
      </aside>
    </section>
    <footer><span><i className="dot cyan"/> DCC / DISASTER INTELLIGENCE <b>PROTOTIPE v0.1</b></span><span>Sumber cuaca: <a href="https://data.bmkg.go.id/" target="_blank" rel="noreferrer">BMKG <ExternalLink size={10}/></a> · Aset: impor pengguna · {demo?"SIMULASI":"Status sesuai kesehatan data"}</span><button onClick={()=>download("dcc-laporan-paparan.json",{ generatedAt:new Date().toISOString(), mode:demo?"simulation":"bmkg", scope:"Sumatera Barat", health:nowcastHealth, corridorMeters:corridor, analysisError:analysis.error, warnings:warnings.map(({areas,...w})=>({...w,geometryCount:areas.length})), exposed:exposed.map(r=>({asset:assetName(r.asset),warningIds:r.warnings.map(w=>w.id)})), reviewed:[...acked] })}><ArrowDownToLine size={12}/> Ekspor laporan sesi</button></footer>
    {notice&&<div className="toast" role="status"><span>{notice}</span><button aria-label="Tutup pemberitahuan" onClick={()=>setNotice("")}><X size={15}/></button></div>}
    {settings&&<div className="modal-backdrop" onClick={()=>setSettings(false)}><section className="settings-modal" role="dialog" aria-modal="true" aria-label="Pengaturan dan konektor" onClick={e=>e.stopPropagation()}><button className="close" aria-label="Tutup pengaturan" onClick={()=>setSettings(false)}><X size={19}/></button><span className="eyebrow">PENGATURAN / KONEKTOR</span><h2>Fondasi pemantauan jaringan</h2><p>Wilayah: seluruh Sumatera Barat. Aset yang diimpor diproses dalam sesi browser dan hilang ketika halaman dimuat ulang. Tidak dikirim ke server.</p><label className="setting-label">Koridor analisis ruas jaringan <select value={corridor} onChange={e=>setCorridor(Number(e.target.value))}>{[0,50,100,250,500].map(n=><option key={n} value={n}>{n} meter</option>)}</select></label><small>Koridor merupakan toleransi spasial untuk jalur, bukan radius bahaya resmi. Titik gardu/tiang diperiksa pada koordinatnya.</small><div className="connector-card"><b>BMKG Open Data</b><p>CAP resmi, prakiraan per 3 jam dan gempa. Konektor memakai cache 60 detik, timeout dan pembatasan akses per instans.</p><a href="https://data.bmkg.go.id/peringatan-dini-cuaca/" target="_blank" rel="noreferrer">Dokumentasi BMKG <ArrowUpRight size={13}/></a></div><div className="connector-card"><b>Google Earth → KML/KMZ</b><p>Ekspor proyek dari Google Earth lalu impor di dashboard. Prototipe belum tersambung langsung ke proyek atau citra Google.</p><a href={EARTH_URL} target="_blank" rel="noreferrer">Buka proyek baseline <ArrowUpRight size={13}/></a></div><div className="connector-card"><b>Radar, petir & waktu tiba hujan</b><p>Belum terhubung. Perlu feed radar bergeoreferensi atau nowcast berlisensi; prakiraan 3 jam tidak dapat menggantikannya.</p><a href="https://www.xweather.com/docs/weather-api/endpoints/lightning" target="_blank" rel="noreferrer">Opsi data petir Xweather <ArrowUpRight size={13}/></a></div><div className="connector-card"><b>SCADA / histori gangguan</b><p>Belum terhubung. Rancangan produksi: salinan data baca saja melalui DMZ, dengan pemetaan ID aset ke penyulang dan tag SCADA.</p></div><button className="primary" onClick={()=>setSettings(false)}>Simpan pengaturan sesi <Check size={15}/></button></section></div>}
  </main>;
}
