/* Disaster Information Center — frontend (vanilla JS + Leaflet, tanpa build) */
'use strict';

/* ================================================================ utils */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf = new Intl.NumberFormat('id-ID');
const num = (v) => (v === null || v === undefined || v === '') ? '—' : nf.format(v);
const TZ = 'Asia/Jakarta';
function fmtTime(iso, opts = {}) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  const o = { timeZone: TZ, hour: '2-digit', minute: '2-digit' };
  if (opts.date !== false) Object.assign(o, { day: '2-digit', month: 'short' });
  if (opts.year) o.year = 'numeric';
  return new Intl.DateTimeFormat('id-ID', o).format(d).replace(/\./g, ':') + ' WIB';
}
function ago(iso) {
  if (!iso) return 'belum pernah';
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return 'baru saja';
  if (s < 3600) return `${Math.round(s / 60)} menit lalu`;
  if (s < 86400) return `${Math.round(s / 3600)} jam lalu`;
  return fmtTime(iso);
}
function toLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date();
  const p = new Intl.DateTimeFormat('sv-SE', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(d);
  return p.replace(' ', 'T');
}
const fromLocalInput = (v) => v ? `${v}:00+07:00` : null;

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: { 'X-DCC': '1' }, credentials: 'same-origin' };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  if (init.method === 'GET') delete init.headers['X-DCC'];
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new Error('Server tidak dapat dihubungi. Periksa jaringan.'); }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    const err = new Error((data && data.error) || `Permintaan gagal (HTTP ${res.status}).`);
    err.status = res.status; err.data = data;
    if (data && data.must_change_password) openPasswordModal(true);
    throw err;
  }
  return data;
}
const fail = (e) => toast(e.message || String(e), 'err');

/* ================================================================ state */
const S = {
  cfg: null, user: null, role: 'guest',
  map: null, base: null, layers: {}, layerOn: {}, counts: {},
  alerts: [], health: [], incidents: [], areas: [], analysisSel: new Set(),
  tab: 'layers', draw: null, pick: null, highlight: null, summary: null, tio: null,
};
const can = (role) => ({ guest: 0, user: 1, admin: 2 })[S.role] >= ({ guest: 0, user: 1, admin: 2 })[role];

const KINDS = {
  gi: { label: 'Gardu Induk', color: '#0b3354', r: 7, w: 3, def: true },
  gh: { label: 'Gardu Hubung / GB', color: '#7c3aed', r: 5.5, w: 2, def: true },
  trafo: { label: 'Gardu distribusi (trafo)', color: '#0a6ebd', r: 3.2, w: 1, def: true },
  recloser: { label: 'Recloser', color: '#db2777', r: 4.5, w: 1.5, def: false },
  lbs: { label: 'LBS', color: '#b7791f', r: 3.8, w: 1.2, def: false },
  jtm: { label: 'Jaringan TM (penyulang)', color: '#0284c7', line: true, def: true },
  ulp: { label: 'Batas ULP', color: '#5b7a99', poly: true, def: true },
  pltd: { label: 'PLTD', color: '#ea580c', r: 5, w: 1.5, def: false },
  kantor: { label: 'Kantor UP3/ULP', color: '#059669', r: 5, w: 1.5, def: false },
};
const UP3_COLORS = { PADANG: '#0a6ebd', BUKITTINGGI: '#0e7490', PAYAKUMBUH: '#4f46e5', SOLOK: '#0f766e' };
const SEV_COLOR = { rendah: '#13804b', sedang: '#0a6ebd', tinggi: '#d97706', kritis: '#c81e1e' };
const SEV_CAP = { Minor: 'ringan', Moderate: 'sedang', Severe: 'berat', Extreme: 'ekstrem', Unknown: 'tidak diketahui' };
const STATUS_LABEL = { dilaporkan: 'Dilaporkan', diverifikasi: 'Diverifikasi', penanganan: 'Dalam penanganan', selesai: 'Selesai' };
const catOf = (code) => (S.cfg?.categories || []).find(c => c.code === code) || { label: code, color: '#475569' };

/* ================================================================ boot */
async function boot() {
  tickClock(); setInterval(tickClock, 1000);
  try {
    S.cfg = await api('/api/config');
  } catch (e) {
    $('#ribbon-state').textContent = e.message;
    return;
  }
  S.user = S.cfg.user; S.role = S.cfg.role;
  $('#site-title').textContent = S.cfg.title;
  $('#site-subtitle').textContent = S.cfg.subtitle;
  document.title = S.cfg.title;
  initMap();
  renderUser();
  bindTabs();
  renderLeft();
  renderRight();
  await loadSummary();
  for (const [k, def] of Object.entries(KINDS)) if (def.def && kindVisible(k) && S.summary?.counts?.[k]) toggleKind(k, true);
  renderLeft();
  refreshAlerts(); setInterval(refreshAlerts, 60000);
  refreshIncidents(); refreshAreas(); setInterval(() => { if (!S.draw) { refreshIncidents(); refreshAreas(); } }, 90000);
  refreshQuake(); setInterval(refreshQuake, 300000);
  refreshTio(); setInterval(refreshTio, 600000);
  if (S.user?.must_change_password) openPasswordModal(true);
}

function tickClock() {
  const d = new Date();
  $('#clock').textContent = new Intl.DateTimeFormat('id-ID', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(d).replace(/\./g, ':');
  $('#clock-date').textContent = new Intl.DateTimeFormat('id-ID', { timeZone: TZ, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(d) + ', WIB';
}

function kindVisible(k) {
  if (S.role !== 'guest') return true;
  return (S.cfg.settings.guest.layers || []).includes(k);
}

/* ================================================================ map */
function initMap() {
  const map = L.map('map', { preferCanvas: true, zoomControl: false, minZoom: 6, maxZoom: 19 }).setView([-0.85, 100.45], 9);
  L.control.zoom({ position: 'topright' }).addTo(map);
  L.control.scale({ imperial: false, position: 'bottomright' }).addTo(map);
  S.map = map;
  S.canvas = L.canvas({ padding: 0.4, tolerance: 4 });
  setBasemap(localStorageGet('dcc.basemap') || S.cfg.settings.basemaps[0]?.id);
  S.layers.alerts = L.layerGroup().addTo(map);
  S.layers.incidents = L.layerGroup().addTo(map);
  S.layers.areas = L.layerGroup().addTo(map);
  S.layers.highlight = L.layerGroup().addTo(map);
  S.layers.draw = L.layerGroup().addTo(map);
  S.layerOn.alerts = true; S.layerOn.incidents = true; S.layerOn.areas = true;
  map.on('click', onMapClick);
  map.on('zoomend', resizePoints);
  map.on('dblclick', (e) => { if (S.draw) { L.DomEvent.stop(e); finishDraw(); } });
  renderMapTools();
  renderLegend();
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

function setBasemap(id) {
  const bm = S.cfg.settings.basemaps.find(b => b.id === id) || S.cfg.settings.basemaps[0];
  if (!bm) return;
  if (S.base) S.map.removeLayer(S.base);
  S.base = L.tileLayer(bm.url, { attribution: bm.attribution, maxZoom: bm.max_zoom || 19, crossOrigin: false, referrerPolicy: 'strict-origin-when-cross-origin' }).addTo(S.map);
  S.base.bringToBack();
  S.baseId = bm.id;
  localStorageSet('dcc.basemap', bm.id);
}

function renderMapTools() {
  const tools = $('#map-tools');
  tools.innerHTML = `
    <button class="btn btn-ghost btn-sm" id="mt-home" title="Tampilkan seluruh Sumatera Barat">Seluruh Sumbar</button>
    ${can('user') ? `<button class="btn btn-ghost btn-sm" id="mt-inc">Tandai kejadian</button>
    <button class="btn btn-ghost btn-sm" id="mt-area">Gambar area</button>` : ''}
    <a class="btn btn-ghost btn-sm" href="${esc(S.cfg.settings.windy.url)}" target="_blank" rel="noopener" title="Buka Windy.com di tab baru">Windy</a>`;
  $('#mt-home').onclick = () => S.map.fitBounds([[-3.5, 98.6], [0.9, 101.9]]);
  if (can('user')) {
    $('#mt-inc').onclick = () => startPick();
    $('#mt-area').onclick = () => startDraw({});
  }
}

function renderLegend() {
  const items = Object.entries(KINDS).filter(([k]) => S.layerOn[k]).map(([k, d]) =>
    `<div><span class="swatch ${d.line ? 'ln' : d.poly ? 'sq' : ''}" style="background:${d.color};${d.line ? 'width:16px' : ''}"></span>${esc(d.label)}</div>`);
  if (S.layerOn.alerts) items.push(`<div><span class="swatch sq" style="background:#f59e0b;opacity:.7"></span>Area peringatan BMKG</div>`);
  if (S.layerOn.wind) items.push(`<div><span class="wind-arrow">➤</span>Angin 10 m, arah bertiup (Open-Meteo)</div>`);
  if (S.layerOn.tio_pts) items.push(`<div><span class="wind-arrow" style="color:#7c3aed">➤</span>Angin & prakiraan 6 jam per UP3 (Tomorrow.io)</div>`);
  if (S.layerOn.tio_rain) items.push(`<div><span class="rain-ramp"></span>Intensitas hujan saat ini, makin pekat makin deras (Tomorrow.io)</div>`);
  if (S.layerOn.radar) items.push(`<div><span class="swatch sq" style="background:#22c55e"></span>Radar hujan RainViewer</div>`);
  $('#legend').innerHTML = items.join('') || '<div class="muted">Tidak ada lapisan aktif</div>';
}

/* ----------------------------------------------------------- asset layers */
async function toggleKind(kind, on) {
  S.layerOn[kind] = on;
  renderLegend();
  if (!on) { if (S.layers[kind]) S.map.removeLayer(S.layers[kind]); return; }
  if (S.layers[kind]) { S.layers[kind].addTo(S.map); orderLayers(); return; }
  $('#map-loading').hidden = false;
  try {
    const data = await api(`/api/assets/layer/${kind}`);
    S.layers[kind] = buildLayer(kind, data);
    if (S.layerOn[kind]) S.layers[kind].addTo(S.map);
    orderLayers();
  } catch (e) { fail(e); S.layerOn[kind] = false; renderLeft(); }
  finally { $('#map-loading').hidden = true; }
}

function buildLayer(kind, data) {
  const d = KINDS[kind];
  const g = L.layerGroup();
  if (data.type === 'points') {
    for (const [id, lon, lat, name] of data.rows) {
      const m = L.circleMarker([lat, lon], { renderer: S.canvas, radius: kindRadius(kind), color: '#ffffff', weight: d.w, fillColor: d.color, fillOpacity: .95 });
      m.on('click', (e) => { if (S.draw || S.pick) return; L.DomEvent.stop(e); openAssetPopup(id, e.latlng); });
      m.bindTooltip(esc(name), { direction: 'top', offset: [0, -4] });
      g.addLayer(m);
    }
  } else {
    L.geoJSON(data.geojson, {
      renderer: S.canvas,
      interactive: !d.poly,
      style: (f) => d.poly
        ? { color: d.color, weight: 1.5, dashArray: '5 4', fill: true, fillOpacity: 0.03, fillColor: d.color, interactive: false }
        : { color: UP3_COLORS[(f.properties.up3 || '').toUpperCase()] || d.color, weight: 2, opacity: .85 },
      onEachFeature: (f, layer) => {
        layer.bindTooltip(esc(f.properties.name), { sticky: true });
        layer.on('click', (e) => { if (S.draw || S.pick) return; L.DomEvent.stop(e); openAssetPopup(f.properties.id, e.latlng); });
      },
    }).addTo(g);
  }
  return g;
}

function kindRadius(kind) {
  const z = S.map.getZoom();
  const r = KINDS[kind].r;
  return kind === 'trafo' || kind === 'lbs' ? (z < 10 ? r * 0.6 : z < 13 ? r : r * 1.4) : r;
}
function resizePoints() {
  for (const k of ['trafo', 'lbs']) S.layers[k]?.eachLayer(m => m.setRadius(kindRadius(k)));
}

function orderLayers() {
  // Lines and boundaries under points.
  for (const k of ['ulp', 'jtm']) S.layers[k]?.eachLayer?.(l => l.bringToBack?.());
}

async function openAssetPopup(id, latlng) {
  const pop = L.popup({ maxWidth: 320 }).setLatLng(latlng).setContent('<span class="muted">Memuat…</span>').openOn(S.map);
  try {
    const a = await api(`/api/assets/${id}`);
    const p = a.props || {};
    const cust = a.customers === undefined ? '' :
      `<div>Pelanggan: <b class="num">${a.customers && a.customers.count !== null ? num(a.customers.count) : 'belum tersedia'}</b></div>`;
    pop.setContent(`
      <div style="font-weight:600;font-size:14px;margin-bottom:2px">${esc(a.name)}</div>
      <div class="muted small" style="margin-bottom:6px">${esc(a.kind_label)}</div>
      ${a.feeder ? `<div>Penyulang: <b>${esc(a.feeder)}</b></div>` : ''}
      ${a.ulp || a.up3 ? `<div>${esc([a.ulp, a.up3 ? 'UP3 ' + a.up3 : ''].filter(Boolean).join(', '))}</div>` : ''}
      ${a.capacity_kva ? `<div>Kapasitas: <b class="num">${num(a.capacity_kva)} kVA</b></div>` : ''}
      ${a.gardu_id ? `<div class="small muted">LOCATION ${esc(a.gardu_id)}${a.code ? ', ' + esc(a.code) : ''}</div>` : ''}
      ${cust}
      <div class="small muted">Status nyala/padam: belum tersedia</div>
      ${a.exposures?.length ? `<div class="notice warn" style="margin:6px 0 0">Berada di area peringatan: ${a.exposures.map(x => esc(x.title)).join('; ')}</div>` : ''}
      ${S.role !== 'guest' ? `<div style="margin-top:8px"><button class="btn btn-ghost btn-sm" data-asset-detail="${a.id}">Atribut lengkap</button></div>` : ''}`);
    const b = document.querySelector(`[data-asset-detail="${a.id}"]`);
    if (b) b.onclick = () => openAssetSheet(a);
    void p;
  } catch (e) { pop.setContent(esc(e.message)); }
}

function openAssetSheet(a) {
  const rows = Object.entries(a.props || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  sheet({
    title: a.name,
    body: `<dl class="kv">
      <dt>Jenis</dt><dd>${esc(a.kind_label)}</dd>
      <dt>Penyulang</dt><dd>${esc(a.feeder || '—')}</dd>
      <dt>UP3 / ULP</dt><dd>${esc(a.up3 || '—')} / ${esc(a.ulp || '—')}</dd>
      <dt>Kapasitas</dt><dd>${a.capacity_kva ? num(a.capacity_kva) + ' kVA' : '—'}</dd>
      <dt>Koordinat</dt><dd class="num">${a.lat?.toFixed(6)}, ${a.lon?.toFixed(6)}</dd>
      <dt>Kunci sumber</dt><dd>${esc(a.source_key)}</dd>
      <dt>Diperbarui</dt><dd>${fmtTime(a.updated_at, { year: true })} (snapshot #${esc(a.last_snapshot_id)})</dd>
      ${rows}</dl>`,
  });
}

/* ================================================================ user */
function renderUser() {
  const box = $('#user-box');
  if (!S.user) {
    box.innerHTML = `<span class="chip grey">Tamu</span><button class="btn btn-sm" id="btn-login">Masuk</button>`;
    $('#btn-login').onclick = openLogin;
    return;
  }
  const roleLabel = S.user.role === 'admin' ? 'Admin' : 'User';
  box.innerHTML = `
    <div style="text-align:right;line-height:1.15"><b>${esc(S.user.display_name || S.user.username)}</b><br><span class="small muted">${roleLabel}</span></div>
    ${S.user.role === 'admin' ? '<button class="btn btn-ghost btn-sm" id="btn-admin">Administrasi</button>' : ''}
    <button class="iconbtn" id="btn-pw" title="Ganti kata sandi" aria-label="Ganti kata sandi"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></button>
    <button class="btn btn-ghost btn-sm" id="btn-logout">Keluar</button>`;
  $('#btn-logout').onclick = async () => { await api('/api/auth/logout', { method: 'POST' }).catch(() => {}); location.reload(); };
  $('#btn-pw').onclick = () => openPasswordModal(false);
  if ($('#btn-admin')) $('#btn-admin').onclick = () => openAdmin('users');
}

function modal(html) {
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !back.dataset.locked) close(); });
  setTimeout(() => $('input', back)?.focus(), 30);
  return { el: back, close };
}

function openLogin() {
  const m = modal(`
    <h2>Masuk</h2><p class="muted small" style="margin:0 0 14px">Akun dibuat oleh Admin DCC. Tamu dapat melihat peta tanpa masuk.</p>
    <form id="f-login">
      <label class="f"><span>Nama pengguna</span><input type="text" name="username" autocomplete="username" required></label>
      <label class="f"><span>Kata sandi</span><input type="password" name="password" autocomplete="current-password" required></label>
      <div class="notice err" id="login-err" hidden></div>
      <div class="row" style="justify-content:flex-end;margin-top:6px"><button type="button" class="btn btn-ghost" id="l-cancel">Batal</button><button class="btn">Masuk</button></div>
    </form>`);
  $('#l-cancel', m.el).onclick = m.close;
  $('#f-login', m.el).onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api('/api/auth/login', { method: 'POST', body: { username: fd.get('username'), password: fd.get('password') } });
      location.reload();
    } catch (err) { const el = $('#login-err', m.el); el.hidden = false; el.textContent = err.message; }
  };
}

function openPasswordModal(forced) {
  if ($('#f-pw')) return;
  const m = modal(`
    <h2>Ganti kata sandi</h2>
    <p class="muted small" style="margin:0 0 14px">${forced ? 'Kata sandi awal wajib diganti sebelum memakai fitur kerja.' : 'Minimal 8 karakter.'}</p>
    <form id="f-pw">
      <label class="f"><span>Kata sandi saat ini</span><input type="password" name="old" autocomplete="current-password" required></label>
      <label class="f"><span>Kata sandi baru</span><input type="password" name="new" minlength="8" autocomplete="new-password" required></label>
      <label class="f"><span>Ulangi kata sandi baru</span><input type="password" name="new2" minlength="8" autocomplete="new-password" required></label>
      <div class="notice err" id="pw-err" hidden></div>
      <div class="row" style="justify-content:flex-end">${forced ? '' : '<button type="button" class="btn btn-ghost" id="pw-cancel">Batal</button>'}<button class="btn">Simpan kata sandi</button></div>
    </form>`);
  if (forced) m.el.dataset.locked = '1';
  if ($('#pw-cancel', m.el)) $('#pw-cancel', m.el).onclick = m.close;
  $('#f-pw', m.el).onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const err = $('#pw-err', m.el);
    if (fd.get('new') !== fd.get('new2')) { err.hidden = false; err.textContent = 'Ulangan kata sandi tidak sama.'; return; }
    try {
      await api('/api/me/password', { method: 'POST', body: { old_password: fd.get('old'), new_password: fd.get('new') } });
      toast('Kata sandi disimpan.', 'ok');
      m.close();
      if (forced) location.reload();
    } catch (x) { err.hidden = false; err.textContent = x.message; }
  };
}

/* ================================================================ sheets */
function sheet({ title, body, foot = '', wide = false, onClose }) {
  closeSheet();
  const back = document.createElement('div');
  back.className = 'sheet-back';
  back.id = 'sheet';
  back.innerHTML = `<div class="sheet ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="sheet-head"><h2>${esc(title)}</h2><button class="iconbtn" data-close aria-label="Tutup"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
    <div class="sheet-body">${body}</div>${foot ? `<div class="sheet-foot">${foot}</div>` : ''}</div>`;
  document.body.appendChild(back);
  const close = () => { back.remove(); onClose && onClose(); };
  back._close = close;
  $('[data-close]', back).onclick = close;
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  back.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  setTimeout(() => $('[data-close]', back).focus(), 20);
  return back;
}
function closeSheet() { const s = $('#sheet'); if (s) s._close ? s._close() : s.remove(); }

/* ================================================================ tabs */
function bindTabs() {
  for (const t of $$('.tab')) t.onclick = () => { S.tab = t.dataset.tab; $$('.tab').forEach(x => x.setAttribute('aria-selected', x === t)); renderLeft(); };
}

function renderLeft() {
  const el = $('#left');
  if (S.tab === 'layers') el.innerHTML = leftLayers();
  if (S.tab === 'incidents') el.innerHTML = leftIncidents();
  if (S.tab === 'areas') el.innerHTML = leftAreas();
  bindLeft();
}

function leftLayers() {
  const sum = S.summary;
  const kinds = Object.entries(KINDS).map(([k, d]) => {
    const vis = kindVisible(k);
    const cnt = sum?.counts?.[k];
    return `<label class="layer ${vis ? '' : 'off'}" title="${vis ? '' : 'Tidak dipublikasikan untuk tamu'}">
      <input type="checkbox" data-kind="${k}" ${S.layerOn[k] ? 'checked' : ''} ${vis && cnt ? '' : 'disabled'}>
      <span class="swatch ${d.line ? 'ln' : d.poly ? 'sq' : ''}" style="background:${d.color}"></span>
      <span>${esc(d.label)}</span><span class="count">${cnt ? num(cnt) : '0'}</span></label>`;
  }).join('');
  const st = S.cfg.settings;
  const wx = [
    ['alerts', 'Area peringatan BMKG', '#f59e0b', true],
    ['quake', 'Gempa terakhir (BMKG)', '#c81e1e', true],
    ['radar', 'Radar hujan 2 jam terakhir (RainViewer)', '#22c55e', st.rainviewer.enabled],
    ['wind', 'Angin dan indikasi konvektif (Open-Meteo)', '#0a4f86', st.openmeteo.enabled],
    ['aqi', 'Kualitas udara US AQI (Open-Meteo CAMS)', '#a3a3a3', st.openmeteo.enabled],
    ['tio_rain', 'Intensitas hujan saat ini (Tomorrow.io)', '#38bdf8', st.tomorrow?.enabled && st.tomorrow?.rain_tiles],
    ['tio_pts', 'Angin & prakiraan hujan 6 jam per UP3 (Tomorrow.io)', '#7c3aed', st.tomorrow?.enabled],
  ].map(([k, label, color, en]) => `<label class="layer ${en ? '' : 'off'}" title="${en ? '' : 'Belum diaktifkan Admin'}">
      <input type="checkbox" data-wx="${k}" ${S.layerOn[k] ? 'checked' : ''} ${en ? '' : 'disabled'}>
      <span class="swatch sq" style="background:${color}"></span><span>${esc(label)}</span><span class="count">${en ? '' : 'nonaktif'}</span></label>`).join('');
  const bms = st.basemaps.map(b => `<option value="${esc(b.id)}" ${b.id === S.baseId ? 'selected' : ''}>${esc(b.name)}</option>`).join('');
  const snap = sum?.snapshot;
  const cs = sum?.customer_set;
  return `
    <div class="sec">
      <input type="search" id="q" placeholder="Cari gardu, penyulang, kode, LOCATION…" autocomplete="off" aria-label="Cari aset">
      <div class="list" id="q-res" style="margin-top:6px"></div>
    </div>
    <div class="sec"><h2>Aset jaringan</h2>${kinds}
      ${sum && !Object.keys(sum.counts || {}).length ? `<div class="empty" style="margin-top:8px">${can('admin')
        ? 'Belum ada aset di database. Impor berkas KML jaringan terlebih dahulu.<br><button class="btn btn-sm" id="btn-go-import" style="margin-top:8px">Impor aset sekarang</button>'
        : S.role === 'guest' ? 'Belum ada aset yang dipublikasikan.' : 'Belum ada aset. Hubungi Admin DCC untuk mengimpor data aset.'}</div>` : ''}
    </div>
    <div class="sec"><h2>Cuaca</h2>${wx}
      <p class="hint">Lapisan cuaca hanya untuk kesadaran situasi. Warna cuaca tidak dipakai untuk menyimpulkan gardu padam.</p></div>
    <div class="sec"><h2>Peta dasar</h2><select id="basemap" aria-label="Peta dasar">${bms}</select></div>
    <div class="sec"><h2>Sumber data</h2>
      <div class="small muted">${snap ? `Aset: snapshot #${snap.id} dari ${esc(snap.source_name || snap.source)}, ${fmtTime(snap.completed_at, { year: true })}` : 'Aset: belum ada snapshot'}</div>
      ${S.role !== 'guest' ? `<div class="small muted">${cs ? `Pelanggan: ${esc(cs.filename)} (${num(cs.with_data)} gardu berdata, total ${num(cs.total_customers)})` : 'Pelanggan: belum diunggah'}</div>` : ''}
      <div class="small muted">Status gardu nyala/padam: belum tersedia (konektor Marco ditunda)</div>
    </div>`;
}

function leftIncidents() {
  const cats = S.cfg.categories.map(c => `<option value="${esc(c.code)}">${esc(c.label)}</option>`).join('');
  const f = S.incFilter || {};
  const list = S.incidents.filter(i => (!f.status || i.status === f.status) && (!f.category || i.category === f.category)
    && (!f.q || (i.title + ' ' + (i.description || '')).toLowerCase().includes(f.q.toLowerCase())));
  return `
    ${can('user') ? '<div class="sec"><button class="btn btn-block" id="btn-new-inc">Tandai kejadian di peta</button><p class="hint">Klik tombol, lalu klik lokasi kejadian pada peta.</p></div>' : ''}
    <div class="sec">
      <div class="grid2">
        <label class="f"><span>Status</span><select id="f-status"><option value="">Semua</option>${Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}" ${f.status === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label class="f"><span>Kategori</span><select id="f-cat"><option value="">Semua</option>${cats}</select></label>
      </div>
      <input type="search" id="f-q" placeholder="Cari judul atau deskripsi" value="${esc(f.q || '')}">
    </div>
    <div class="sec"><h2>Kejadian <span class="aside">${list.length} dari ${S.incidents.length}</span></h2>
      <div class="list">${list.map(incidentItem).join('') || `<div class="empty">${S.incidents.length ? 'Tidak ada kejadian yang cocok dengan filter.' : 'Belum ada kejadian tercatat.'}</div>`}</div>
    </div>`;
}

function incidentItem(i) {
  const c = catOf(i.category);
  return `<div class="item" style="--c:${c.color}" data-inc="${i.id}" tabindex="0">
    <h3>${esc(i.title)}</h3>
    <div class="meta"><span>${esc(c.label)}</span><span>${fmtTime(i.occurred_at)}</span>
      <span class="chip ${i.status === 'selesai' ? 'ok' : i.severity === 'kritis' || i.severity === 'tinggi' ? 'warn' : ''}">${STATUS_LABEL[i.status] || i.status}</span>
      ${i.photo_count ? `<span>${i.photo_count} foto</span>` : ''}${i.area_count ? `<span>${i.area_count} area</span>` : ''}</div></div>`;
}

function leftAreas() {
  const sel = S.analysisSel;
  const incName = (id) => S.incidents.find(i => i.id === id)?.title;
  return `
    ${can('user') ? `<div class="sec"><button class="btn btn-block" id="btn-draw">Gambar area dampak</button>
      <p class="hint">Klik titik-titik sudut pada peta, klik ganda atau tekan Selesai untuk menutup polygon.</p></div>` : ''}
    <div class="sec"><h2>Area tersimpan <span class="aside">${S.areas.length}</span></h2>
      <div class="list">${S.areas.map(a => `
        <div class="item ${sel.has(a.id) ? 'sel' : ''}" style="--c:${esc(a.color)}" data-area="${a.id}">
          <div class="row">
            ${can('user') ? `<input type="checkbox" data-area-sel="${a.id}" ${sel.has(a.id) ? 'checked' : ''} aria-label="Pilih untuk rekap">` : ''}
            <div class="grow"><h3>${esc(a.name)}</h3>
              <div class="meta"><span class="num">${num(Math.round((a.area_km2 || 0) * 100) / 100)} km²</span>${a.incident_id ? `<span>${esc(incName(a.incident_id) || 'Kejadian #' + a.incident_id)}</span>` : '<span>Tanpa kejadian</span>'}<span>rev ${a.revision}</span></div></div>
          </div></div>`).join('') || '<div class="empty">Belum ada area. Gambar polygon dampak untuk menghitung gardu dan pelanggan di dalamnya.</div>'}</div>
    </div>
    ${can('user') ? `<div class="sec" style="position:sticky;bottom:0;background:#fff;padding-top:8px">
      <button class="btn btn-block" id="btn-analyze" ${sel.size ? '' : 'disabled'}>Hitung rekap ${sel.size ? sel.size + ' area terpilih' : ''}</button>
      <p class="hint">Gardu yang berada di beberapa area dihitung satu kali.</p></div>` : ''}`;
}

function bindLeft() {
  const el = $('#left');
  $$('[data-kind]', el).forEach(i => i.onchange = () => toggleKind(i.dataset.kind, i.checked));
  $$('[data-wx]', el).forEach(i => i.onchange = () => toggleWeather(i.dataset.wx, i.checked));
  if ($('#btn-go-import', el)) $('#btn-go-import', el).onclick = () => openAdmin('assets');
  if ($('#basemap', el)) $('#basemap', el).onchange = (e) => setBasemap(e.target.value);
  const q = $('#q', el);
  if (q) {
    let t;
    q.oninput = () => { clearTimeout(t); t = setTimeout(() => searchAssets(q.value), 250); };
  }
  if ($('#btn-new-inc', el)) $('#btn-new-inc', el).onclick = () => startPick();
  if ($('#f-status', el)) {
    const f = S.incFilter = S.incFilter || {};
    $('#f-cat', el).value = f.category || '';
    $('#f-status', el).onchange = (e) => { f.status = e.target.value; renderLeft(); };
    $('#f-cat', el).onchange = (e) => { f.category = e.target.value; renderLeft(); };
    let t;
    $('#f-q', el).oninput = (e) => { clearTimeout(t); t = setTimeout(() => { f.q = e.target.value; renderLeft(); $('#f-q').focus(); $('#f-q').setSelectionRange(99, 99); }, 300); };
  }
  $$('[data-inc]', el).forEach(i => {
    i.onclick = () => openIncident(+i.dataset.inc);
    i.onkeydown = (e) => { if (e.key === 'Enter') openIncident(+i.dataset.inc); };
  });
  if ($('#btn-draw', el)) $('#btn-draw', el).onclick = () => startDraw({});
  $$('[data-area-sel]', el).forEach(c => {
    c.onclick = (e) => e.stopPropagation();
    c.onchange = () => { const id = +c.dataset.areaSel; c.checked ? S.analysisSel.add(id) : S.analysisSel.delete(id); renderLeft(); drawAreas(); };
  });
  $$('[data-area]', el).forEach(i => i.onclick = () => {
    const a = S.areas.find(x => x.id === +i.dataset.area);
    if (a) { S.map.fitBounds([[a.bbox[1], a.bbox[0]], [a.bbox[3], a.bbox[2]]], { padding: [40, 40] }); openArea(a); }
  });
  if ($('#btn-analyze', el)) $('#btn-analyze', el).onclick = () => runAnalysis([...S.analysisSel], null);
}

async function searchAssets(q) {
  const box = $('#q-res');
  if (!box) return;
  if (q.trim().length < 2) { box.innerHTML = ''; return; }
  try {
    const r = await api(`/api/assets/search?q=${encodeURIComponent(q)}`);
    box.innerHTML = r.results.map(x => `<div class="item" style="--c:${KINDS[x.kind]?.color}" data-hit="${x.id}"><h3>${esc(x.name)}</h3><div class="meta"><span>${esc(KINDS[x.kind]?.label || x.kind)}</span>${x.feeder ? `<span>${esc(x.feeder)}</span>` : ''}${x.up3 ? `<span>UP3 ${esc(x.up3)}</span>` : ''}</div></div>`).join('')
      || '<div class="empty">Tidak ditemukan.</div>';
    $$('[data-hit]', box).forEach(el => el.onclick = () => {
      const x = r.results.find(y => y.id === +el.dataset.hit);
      if (x.kind === 'jtm' || x.kind === 'ulp') S.map.fitBounds([[x.bbox[1], x.bbox[0]], [x.bbox[3], x.bbox[2]]], { padding: [30, 30] });
      else S.map.setView([x.lat, x.lon], 17);
      if (!S.layerOn[x.kind]) { toggleKind(x.kind, true); renderLeft(); }
      flash([x.lat, x.lon]);
      openAssetPopup(x.id, L.latLng(x.lat, x.lon));
    });
  } catch (e) { fail(e); }
}

function flash(latlng) {
  const c = L.circleMarker(latlng, { radius: 18, color: '#c81e1e', weight: 3, fill: false }).addTo(S.layers.highlight);
  setTimeout(() => S.layers.highlight.removeLayer(c), 4000);
}

async function loadSummary() {
  try { S.summary = await api('/api/assets/summary'); } catch (e) { fail(e); }
  if (S.tab === 'layers') renderLeft();
}

/* ================================================================ weather */
async function refreshAlerts() {
  try {
    const r = await api('/api/weather/alerts');
    S.alerts = r.alerts; S.health = r.health; S.corridor = r.corridor_m;
  } catch (e) {
    S.alerts = []; S.health = [{ source: 'bmkg_cap', state: 'error', error: e.message }];
  }
  renderRibbon(); renderRight(); drawAlerts();
}

function renderRibbon() {
  const rb = $('#ribbon');
  const cap = S.health.find(h => h.source === 'bmkg_cap');
  const real = S.alerts.filter(a => !a.simulated);
  const sim = S.alerts.find(a => a.simulated);
  rb.className = 'ribbon';
  let state, meta = cap ? `Diperiksa ${ago(cap.checked_at)}` : '';
  if (!cap || cap.state === 'unknown') { state = 'Menunggu data BMKG pertama'; }
  else if (cap.state === 'error' && !real.length) {
    rb.classList.add('gagal');
    state = 'Sumber BMKG tidak dapat dihubungi. Status cuaca belum diketahui, bukan berarti aman.';
    meta = cap.ok_at ? `Terakhir berhasil ${ago(cap.ok_at)}` : 'Belum pernah berhasil sejak server dijalankan';
  } else if (real.length) {
    rb.classList.add('siaga');
    state = `${real.length} peringatan BMKG aktif di Sumatera Barat`;
  } else state = 'Tidak ada peringatan BMKG aktif untuk Sumatera Barat';
  if (sim && !real.length) { rb.classList.add('sim'); state = 'Mode simulasi aktif. Area dan paparan berikut fiktif untuk latihan.'; }
  $('#ribbon-state').innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.5 19a4.5 4.5 0 1 0-1.4-8.8A6 6 0 0 0 4 12a4 4 0 0 0 1 7.9z"/>${real.length ? '<path d="m13 11-3 5h4l-3 5"/>' : ''}</svg>${esc(state)}`;
  $('#ribbon-items').innerHTML = S.alerts.map(a => `<button class="ribbon-chip" data-alert="${esc(a.id)}">${a.simulated ? 'SIMULASI: ' : ''}${esc(a.areas_text.slice(0, 2).join(', ') || a.title)}, ${num(a.exposure.trafo || 0)} gardu${a.exposure.jtm ? `, ${a.exposure.jtm} penyulang` : ''}</button>`).join('');
  $('#ribbon-meta').textContent = meta;
  $$('[data-alert]', rb).forEach(b => b.onclick = () => focusAlert(b.dataset.alert));
}

function drawAlerts() {
  const g = S.layers.alerts;
  g.clearLayers();
  if (!S.layerOn.alerts) return;
  for (const a of S.alerts) {
    for (const area of a.areas || []) {
      const layer = L.geoJSON(area, { style: { color: a.simulated ? '#7c3aed' : '#d97706', weight: 2, dashArray: '6 4', fillColor: a.simulated ? '#a78bfa' : '#fbbf24', fillOpacity: .22 } });
      layer.bindTooltip(`${esc(a.title)}`, { sticky: true });
      layer.on('click', (e) => { if (S.draw || S.pick) return; L.DomEvent.stop(e); focusAlert(a.id); });
      g.addLayer(layer);
    }
  }
}

/* ----------------------------------------------- panel kanan yang bisa dilipat */
const RP_DEFAULT = { open: ['alerts', 'quake'], pin: ['alerts'] };
function rpState() {
  if (!S.rp) {
    let v = null;
    try { v = JSON.parse(localStorageGet('dcc.rpanels') || 'null'); } catch { v = null; }
    S.rp = v && Array.isArray(v.open) && Array.isArray(v.pin) ? v : JSON.parse(JSON.stringify(RP_DEFAULT));
  }
  return S.rp;
}
const rpSave = () => localStorageSet('dcc.rpanels', JSON.stringify(S.rp));

function panel(id, title, aside, body) {
  const st = rpState(), open = st.open.includes(id), pin = st.pin.includes(id);
  return `<section class="rp ${open ? 'open' : ''}" data-rp="${id}">
    <div class="rp-h"><button class="rp-t" aria-expanded="${open}"><svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m9 6 6 6-6 6"/></svg><span>${title}</span></button>
      <span class="rp-aside">${aside || ''}</span>
      <label class="rp-pin" title="Tetap terbuka saat membuka bagian lain"><input type="checkbox" ${pin ? 'checked' : ''}>tetap</label></div>
    <div class="rp-b">${body}</div></section>`;
}

// Membuka satu bagian menutup bagian lain yang tidak dicentang "tetap".
function setPanel(id, open) {
  const st = rpState();
  st.open = open ? [...st.open.filter(x => st.pin.includes(x) && x !== id), id] : st.open.filter(x => x !== id);
  rpSave(); applyPanels();
  if (open) $(`#right [data-rp="${id}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function applyPanels() {
  const st = rpState();
  $$('#right [data-rp]').forEach(sec => {
    const id = sec.dataset.rp, o = st.open.includes(id);
    sec.classList.toggle('open', o);
    $('.rp-t', sec).setAttribute('aria-expanded', o);
    $('.rp-pin input', sec).checked = st.pin.includes(id);
  });
}

function bindPanels(el) {
  $$('[data-rp]', el).forEach(sec => {
    const id = sec.dataset.rp;
    $('.rp-t', sec).onclick = () => setPanel(id, !sec.classList.contains('open'));
    $('.rp-pin input', sec).onchange = (e) => {
      const st = rpState();
      st.pin = st.pin.filter(x => x !== id);
      if (e.target.checked) { st.pin.push(id); if (!st.open.includes(id)) st.open.push(id); }
      rpSave(); applyPanels();
    };
  });
}

function renderRight() {
  const el = $('#right');
  const real = S.alerts.filter(a => !a.simulated);
  // Peringatan BMKG baru selalu dibuka otomatis walau bagiannya sedang terlipat.
  if (real.length > (S.lastAlertCount ?? 0) && !rpState().open.includes('alerts')) { S.rp.open.push('alerts'); rpSave(); }
  S.lastAlertCount = real.length;
  const cards = S.alerts.map(alertCard).join('') || `<div class="calm">${(S.health.find(h => h.source === 'bmkg_cap')?.state === 'error')
    ? 'Data peringatan belum dapat diambil. Periksa koneksi internet server ke bmkg.go.id.'
    : 'Tidak ada peringatan dini cuaca BMKG yang berlaku untuk Sumatera Barat saat ini. Tetap pantau kondisi lapangan.'}</div>`;
  const labels = { bmkg_cap: 'BMKG peringatan dini (CAP)', bmkg_quake: 'BMKG gempa terkini', wa_gateway: 'WA gateway', tomorrow: 'Tomorrow.io (prakiraan model)' };
  const health = S.health.map(h => `<div><span>${esc(labels[h.source] || h.source)}</span><span class="chip ${h.state === 'ok' ? 'ok' : h.state === 'partial' ? 'warn' : h.state === 'error' ? 'err' : 'grey'}" title="${esc(h.error || '')}">${{ ok: 'Terhubung', partial: 'Sebagian', error: 'Gagal', unknown: 'Belum dicek' }[h.state] || h.state}</span></div>`).join('')
    || '<div class="muted">Collector belum berjalan.</div>';
  const bad = S.health.filter(h => h.state === 'error').length;
  const alertAside = S.alerts.some(a => a.simulated) && !real.length ? '<span class="chip warn">simulasi</span>'
    : real.length ? `<span class="chip err">${real.length} aktif</span>` : '<span class="chip ok">tidak ada</span>';
  const tioOn = S.cfg.settings.tomorrow?.enabled;
  el.innerHTML = `
    ${panel('alerts', 'Peringatan dini cuaca', alertAside, `${S.corridor !== undefined ? `<div class="small muted" style="margin-bottom:6px">Koridor jaringan ${S.corridor} m</div>` : ''}${cards}
      ${can('user') ? `<div class="row" style="margin-top:6px"><button class="btn btn-ghost btn-sm" id="btn-wx-refresh">Periksa sekarang</button>
      <button class="btn btn-ghost btn-sm" id="btn-sim">${S.alerts.some(a => a.simulated) ? 'Hentikan simulasi' : 'Latihan simulasi'}</button></div>` : ''}`)}
    ${panel('quake', 'Gempa', '<span id="quake-aside"></span>', `<div id="quake-box" class="muted small">Memuat…</div>
      <div class="qh"><div class="qh-head"><b class="small">5 gempa terakhir</b><span class="seg"><button data-qsrc="dirasakan">Dirasakan</button><button data-qsrc="m5">M5+</button></span></div>
      <div id="quake-hist" class="small muted">Memuat…</div></div>`)}
    ${tioOn ? panel('tio', 'Prakiraan hujan 6 jam per UP3', '<span id="tio-aside" class="small muted">Tomorrow.io</span>', '<div id="tio-box" class="small">Memuat…</div>') : ''}
    ${can('user') ? panel('fc', 'Prakiraan desa/kelurahan', `<span class="small muted" id="fc-aside">${esc(S.fcName || 'BMKG')}</span>`, `
      <div class="wil-search"><input type="search" id="wil-q" placeholder="Cari kota, kecamatan, atau desa/nagari…" autocomplete="off" aria-label="Cari wilayah">
        <div class="list" id="wil-res"></div></div>
      <select id="wil-kab" aria-label="Kabupaten/Kota" style="margin-top:6px"><option value="">Kabupaten/Kota…</option></select>
      <select id="wil-kec" aria-label="Kecamatan" style="margin-top:6px" disabled><option value="">Kecamatan…</option></select>
      <select id="wil-desa" aria-label="Desa/Kelurahan" style="margin-top:6px" disabled><option value="">Desa/Kelurahan/Nagari…</option></select>
      <div id="fc-box" class="small" style="margin-top:8px"></div><p class="hint">Prakiraan BMKG 3 harian per desa/kelurahan. Memilih kecamatan otomatis memakai kelurahan/desa pusatnya.</p>`) : ''}
    ${panel('health', 'Kesehatan sumber', bad ? `<span class="chip err">${bad} gagal</span>` : '<span class="chip ok">normal</span>', `<div class="health">${health}</div>`)}
    <div class="sec small muted" style="margin-top:12px">Aplikasi ini mendukung keputusan operator dan tidak menjalankan perintah SCADA atau switching. Tindak lanjut mengikuti SOP DCC dan verifikasi lapangan. Peringatan resmi: BMKG. Prakiraan tambahan: Tomorrow.io/Open-Meteo (model).</div>`;
  bindPanels(el);
  $$('[data-alert-focus]', el).forEach(b => b.onclick = () => focusAlert(b.dataset.alertFocus));
  $$('[data-alert-list]', el).forEach(b => b.onclick = () => openAlertAssets(b.dataset.alertList));
  $$('[data-alert-review]', el).forEach(b => b.onclick = () => reviewAlert(b.dataset.alertReview));
  if ($('#btn-wx-refresh')) $('#btn-wx-refresh').onclick = async () => { await api('/api/weather/refresh', { method: 'POST' }).catch(fail); toast('Pemeriksaan BMKG dijalankan.'); setTimeout(refreshAlerts, 6000); };
  if ($('#btn-sim')) $('#btn-sim').onclick = async () => {
    const on = !S.alerts.some(a => a.simulated);
    await api('/api/weather/simulate', { method: 'POST', body: { on } }).catch(fail);
    await refreshAlerts();
    if (on) focusAlert('SIMULASI-CAP-001');
  };
  $$('[data-qsrc]', el).forEach(b => b.onclick = () => { S.qsrc = b.dataset.qsrc; localStorageSet('dcc.qsrc', S.qsrc); renderQuakeHist(); });
  if ($('#wil-kab')) initWilayah();
  renderQuake();
  renderQuakeHist();
  renderTio();
}

function alertCard(a) {
  const ex = a.exposure || {};
  const reviewed = a.reviews?.length ? `<div class="small" style="margin-top:6px">Ditinjau ${esc(a.reviews.at(-1).user_name)}, ${fmtTime(a.reviews.at(-1).reviewed_at)}</div>` : '';
  return `<div class="alert-card ${a.simulated ? 'sim' : ''}">
    <h3>${esc(a.title)}</h3>
    <div class="meta">Berlaku ${fmtTime(a.effective)} sampai ${fmtTime(a.expires)}${a.severity ? ', tingkat ' + esc(SEV_CAP[a.severity] || a.severity) : ''}</div>
    <div class="small" style="margin-top:4px">${esc(a.areas_text.join(', '))}</div>
    <div class="stats"><div class="stat"><b>${num(ex.trafo || 0)}</b><span>gardu distribusi</span></div>
      <div class="stat"><b>${num(ex.jtm || 0)}</b><span>penyulang</span></div>
      <div class="stat"><b>${num((ex.recloser || 0) + (ex.lbs || 0) + (ex.gh || 0) + (ex.gi || 0))}</b><span>GI/GH/LBS/recloser</span></div></div>
    ${a.instruction ? `<div class="small">${esc(a.instruction)}</div>` : ''}
    <div class="row wrap" style="margin-top:8px"><button class="btn btn-sm" data-alert-focus="${esc(a.id)}">Lihat di peta</button>
      ${can('user') ? `<button class="btn btn-ghost btn-sm" data-alert-list="${esc(a.id)}">Daftar aset terpapar</button>
      ${a.simulated ? '' : `<button class="btn btn-ghost btn-sm" data-alert-review="${esc(a.id)}">Tandai ditinjau</button>`}` : ''}</div>
    ${reviewed}</div>`;
}

async function focusAlert(id) {
  const a = S.alerts.find(x => x.id === id);
  if (!a) return;
  if (!S.layerOn.alerts) { S.layerOn.alerts = true; drawAlerts(); }
  const b = L.geoJSON({ type: 'GeometryCollection', geometries: a.areas }).getBounds();
  if (b.isValid()) S.map.fitBounds(b, { padding: [30, 30] });
  S.layers.highlight.clearLayers();
  if (!can('user')) return;
  try {
    const r = await api(`/api/weather/alerts/${encodeURIComponent(id)}/assets`);
    for (const x of r.assets) {
      if (x.kind === 'jtm') continue;
      L.circleMarker([x.lat, x.lon], { renderer: S.canvas, radius: 4.5, color: '#c81e1e', weight: 1.5, opacity: .8, fill: false, interactive: false }).addTo(S.layers.highlight);
    }
  } catch (e) { fail(e); }
}

async function openAlertAssets(id) {
  try {
    const r = await api(`/api/weather/alerts/${encodeURIComponent(id)}/assets`);
    const a = S.alerts.find(x => x.id === id) || { title: id };
    const feeders = {};
    for (const x of r.assets) if (x.kind === 'trafo') feeders[x.feeder || '(tanpa penyulang)'] = (feeders[x.feeder || '(tanpa penyulang)'] || 0) + 1;
    sheet({
      title: `Aset terpapar: ${a.title}`, wide: true,
      body: `<div class="notice">Paparan peringatan berarti aset berada di dalam area CAP BMKG (penyulang memakai koridor ${S.corridor} m). Ini bukan bukti cuaca buruk sedang terjadi di setiap titik atau gardu padam.</div>
        <h3>Per penyulang</h3><div class="tablebox"><table class="t"><thead><tr><th>Penyulang</th><th class="n">Gardu</th></tr></thead><tbody>
        ${Object.entries(feeders).sort((x, y) => y[1] - x[1]).map(([f, n]) => `<tr><td>${esc(f)}</td><td class="n">${n}</td></tr>`).join('')}</tbody></table></div>
        <h3>Semua aset (${r.assets.length})</h3><div class="tablebox"><table class="t"><thead><tr><th>Nama</th><th>Jenis</th><th>Penyulang</th><th>ULP</th><th class="n">kVA</th></tr></thead><tbody>
        ${r.assets.map(x => `<tr><td>${esc(x.name)}</td><td>${esc(KINDS[x.kind]?.label || x.kind)}</td><td>${esc(x.feeder || '')}</td><td>${esc(x.ulp || '')}</td><td class="n">${x.kva ? num(x.kva) : ''}</td></tr>`).join('')}</tbody></table></div>`,
    });
  } catch (e) { fail(e); }
}

async function reviewAlert(id) {
  const note = prompt('Catatan peninjauan (opsional):', '');
  if (note === null) return;
  try { await api(`/api/weather/alerts/${encodeURIComponent(id)}/review`, { method: 'POST', body: { note } }); toast('Peringatan ditandai sudah ditinjau.', 'ok'); refreshAlerts(); } catch (e) { fail(e); }
}

async function refreshQuake() {
  const [q, h] = await Promise.all([api('/api/weather/quake').catch(() => null), api('/api/weather/quakes').catch(() => null)]);
  S.quake = q; S.quakes = h;
  renderQuake(); renderQuakeHist();
  if (S.layerOn.quake) drawQuake();
}

function renderQuakeHist() {
  const box = $('#quake-hist');
  if (!box) return;
  S.qsrc = S.qsrc || localStorageGet('dcc.qsrc') || 'dirasakan';
  $$('[data-qsrc]').forEach(b => b.classList.toggle('on', b.dataset.qsrc === S.qsrc));
  const list = S.quakes?.lists?.[S.qsrc];
  if (!list?.items?.length) { box.textContent = S.quakes ? 'Daftar gempa BMKG belum tersedia.' : 'Memuat…'; return; }
  S.qOpen = S.qOpen || new Set();
  const items = list.items.slice(0, 5);
  box.className = 'small';
  box.innerHTML = items.map((g, i) => {
    const key = g.datetime + g.wilayah;
    const mag = parseFloat(g.magnitude);
    return `<details class="qi" data-qk="${esc(key)}" ${S.qOpen.has(key) ? 'open' : ''}>
      <summary><span class="qm ${mag >= 6 ? 'hi' : mag >= 5 ? 'md' : ''}">${esc(g.magnitude)}</span><span class="qw">${esc(g.wilayah)}</span><span class="qt">${ago(g.datetime)}</span></summary>
      <div class="qd">
        <div>${esc(g.tanggal)} ${esc(g.jam)}</div>
        <div>Kedalaman ${esc(g.kedalaman)}, ${esc(g.lat)}, ${esc(g.lon)}</div>
        <div>±${num(g.km_padang)} km dari Kota Padang</div>
        ${g.dirasakan ? `<div>Dirasakan: ${esc(g.dirasakan)}</div>` : ''}${g.potensi ? `<div>${esc(g.potensi)}</div>` : ''}
        <button class="btn btn-ghost btn-sm" data-qmap="${i}" style="margin-top:4px">Lihat di peta</button>
      </div></details>`;
  }).join('') + `<p class="hint">Sumber BMKG, ${S.qsrc === 'm5' ? 'gempa M5,0+ terkini' : 'gempa dirasakan terkini'} seluruh Indonesia.</p>`;
  $$('details.qi', box).forEach(d => d.ontoggle = () => { d.open ? S.qOpen.add(d.dataset.qk) : S.qOpen.delete(d.dataset.qk); });
  $$('[data-qmap]', box).forEach(b => b.onclick = () => {
    const g = items[+b.dataset.qmap];
    if (S.layers.qsel) S.map.removeLayer(S.layers.qsel);
    S.layers.qsel = L.circleMarker([g.lat, g.lon], { radius: 9, color: '#fff', weight: 2, fillColor: '#7f1d1d', fillOpacity: 1 })
      .bindTooltip(`M${esc(g.magnitude)} ${esc(g.wilayah)}<br>${esc(g.tanggal)} ${esc(g.jam)}<br><span style="color:#666">klik titik untuk menutup</span>`, { permanent: true, direction: 'top' }).addTo(S.map);
    S.layers.qsel.on('click', () => { S.map.removeLayer(S.layers.qsel); S.layers.qsel = null; });
    S.map.setView([g.lat, g.lon], 7);
  });
}
function renderQuake() {
  const box = $('#quake-box');
  if (!box) return;
  const q = S.quake?.detail;
  if (!q) { box.textContent = S.quake?.state === 'error' ? 'Data gempa BMKG gagal diambil.' : 'Belum ada data gempa.'; return; }
  box.className = '';
  if ($('#quake-aside')) $('#quake-aside').innerHTML = `<span class="small muted">M${esc(q.magnitude)} · ${esc(q.jam || '')}</span>`;
  box.innerHTML = `<div class="quake"><div class="mag">${esc(q.magnitude)}</div><div><b>${esc(q.wilayah)}</b></div>
    <div class="small muted">${esc(q.tanggal)} ${esc(q.jam)}, kedalaman ${esc(q.kedalaman)}</div></div>
    <div class="small" style="margin-top:6px">${esc(q.potensi || '')}${q.dirasakan ? '. Dirasakan: ' + esc(q.dirasakan) : ''}</div>
    <p class="hint">Magnitudo dan jarak saja tidak cukup untuk menyimpulkan kerusakan gardu. Diperiksa ${ago(S.quake.checked_at)}.</p>`;
}
function drawQuake() {
  if (S.layers.quake) S.map.removeLayer(S.layers.quake);
  const q = S.quake?.detail;
  if (!q) return;
  S.layers.quake = L.layerGroup([
    L.circle([q.lat, q.lon], { radius: 50000, color: '#c81e1e', weight: 1, fillOpacity: .06 }),
    L.circleMarker([q.lat, q.lon], { radius: 8, color: '#fff', weight: 2, fillColor: '#c81e1e', fillOpacity: 1 }).bindTooltip(`M${esc(q.magnitude)} ${esc(q.wilayah)}`),
  ]).addTo(S.map);
}

/* ------------------------------------------------ wilayah Sumbar + prakiraan BMKG */
async function loadWilayah() {
  if (S.wil) return S.wil;
  const d = await (await fetch('/static/wilayah_sumbar.json')).json();
  const norm = (t) => t.toLowerCase().replace(/^(kabupaten|kota)\s+/, '');
  const byCode = {};
  for (const [k, n] of d.kab) byCode[k] = { k, n, lvl: 1 };
  for (const [k, n] of d.kec) byCode[k] = { k, n, lvl: 2 };
  for (const [k, n] of d.desa) byCode[k] = { k, n, lvl: 3, kel: k.split('.')[3][0] === '1' };
  const all = Object.values(byCode).map(w => ({ ...w, s: norm(w.n) }));
  S.wil = { ...d, byCode, all, kecOf: (kab) => d.kec.filter(([k]) => k.startsWith(kab + '.')), desaOf: (kec) => d.desa.filter(([k]) => k.startsWith(kec + '.')) };
  return S.wil;
}

function wilLabel(code) {
  const w = S.wil.byCode, p = code.split('.');
  return [w[code]?.n, p.length > 2 && code !== p.slice(0, 3).join('.') ? 'Kec. ' + w[p.slice(0, 3).join('.')]?.n : null,
    p.length > 1 && code !== p.slice(0, 2).join('.') ? w[p.slice(0, 2).join('.')]?.n : null].filter(Boolean).join(', ');
}

// Desa "pusat" kecamatan: kelurahan/desa yang namanya sama dengan kecamatan, lalu kelurahan pertama, lalu desa pertama.
function defaultDesa(kec) {
  const list = S.wil.desaOf(kec), name = S.wil.byCode[kec]?.n.toLowerCase();
  return (list.find(([, n]) => n.toLowerCase() === name) || list.find(([k]) => k.split('.')[3][0] === '1') || list[0])?.[0];
}

async function initWilayah() {
  const kab = $('#wil-kab'), kec = $('#wil-kec'), desa = $('#wil-desa'), q = $('#wil-q'), res = $('#wil-res');
  try { await loadWilayah(); } catch (e) { $('#fc-box').textContent = 'Daftar wilayah gagal dimuat.'; return; }
  const opts = (rows, ph) => `<option value="">${ph}</option>` + rows.map(([k, n]) => `<option value="${k}">${esc(n)}</option>`).join('');
  kab.innerHTML = opts(S.wil.kab, 'Kabupaten/Kota…');
  const setTo = (code, load = true) => {
    const p = code.split('.');
    kab.value = p.slice(0, 2).join('.');
    kec.innerHTML = opts(S.wil.kecOf(kab.value), 'Kecamatan…'); kec.disabled = false;
    if (p.length < 3) { desa.innerHTML = opts([], 'Desa/Kelurahan/Nagari…'); desa.disabled = true; return; }
    kec.value = p.slice(0, 3).join('.');
    desa.innerHTML = opts(S.wil.desaOf(kec.value), 'Desa/Kelurahan/Nagari…'); desa.disabled = false;
    desa.value = p.length > 3 ? code : defaultDesa(kec.value);
    if (load && desa.value) loadForecast(desa.value);
  };
  kab.onchange = () => kab.value ? setTo(kab.value) : (kec.disabled = desa.disabled = true);
  kec.onchange = () => kec.value && setTo(kec.value);
  desa.onchange = () => desa.value && loadForecast(desa.value);
  q.oninput = () => {
    const t = q.value.trim().toLowerCase();
    if (t.length < 3) { res.innerHTML = ''; return; }
    const hits = S.wil.all.filter(w => w.s.includes(t)).sort((a, b) => a.lvl - b.lvl || (b.s.startsWith(t) - a.s.startsWith(t)) || a.n.localeCompare(b.n)).slice(0, 12);
    const tag = { 1: 'Kab/Kota', 2: 'Kecamatan', 3: 'Desa/Kel.' };
    res.innerHTML = hits.map(w => `<button class="item" data-wil="${w.k}"><b>${esc(w.n)}</b> <span class="muted small">${tag[w.lvl]}${w.lvl > 1 ? ' · ' + esc(wilLabel(w.k).split(', ').slice(1).join(', ')) : ''}</span></button>`).join('') || '<div class="muted small">Tidak ditemukan.</div>';
    $$('[data-wil]', res).forEach(b => b.onclick = () => { setTo(b.dataset.wil); res.innerHTML = ''; q.value = ''; });
  };
  const saved = localStorageGet('dcc.adm4');
  if (saved && S.wil.byCode[saved]) setTo(saved, !S.fcHtml); // pulihkan pilihan terakhir
  if (S.fcHtml) $('#fc-box').innerHTML = S.fcHtml; // panel kanan digambar ulang tiap menit
}

async function loadForecast(code) {
  const box = $('#fc-box');
  if (!box || !code) return;
  localStorageSet('dcc.adm4', code);
  box.textContent = 'Memuat…';
  try {
    // Sebagian kode Kepmendagri terbaru belum dikenal BMKG: coba desa lain di kecamatan yang sama.
    const kec = code.split('.').slice(0, 3).join('.');
    const tries = [code, ...(S.wil ? S.wil.desaOf(kec).map(([k]) => k).filter(k => k !== code) : [])].slice(0, 5);
    let r = null, used = code, lastErr = null;
    for (const c of tries) {
      try { r = await api(`/api/weather/forecast?adm4=${encodeURIComponent(c)}`); used = c; break; }
      catch (e) { lastErr = e; if (e.status && e.status !== 404) break; }
    }
    if (!r) throw lastErr || new Error('Prakiraan tidak tersedia.');
    const lok = r.lokasi || {};
    const note = used !== code ? `<div class="hint" style="margin:2px 0">BMKG belum punya data untuk ${esc(S.wil.byCode[code]?.n || code)}, ditampilkan desa terdekat di kecamatan yang sama.</div>` : '';
    const rows = (r.data?.[0]?.cuaca || []).flat().slice(0, 8);
    box.innerHTML = `<b>${esc(lok.desa || S.wil?.byCode[code]?.n || '')}, ${esc(lok.kecamatan || '')}</b><div class="muted">${esc(lok.kotkab || '')}</div>${note}<table class="t" style="margin-top:4px"><tbody>${rows.map(c => `<tr><td>${fmtTime(c.utc_datetime + 'Z')}</td><td>${esc(c.weather_desc)}</td><td class="n">${esc(c.t)}°C</td><td class="n">${esc(c.ws)} km/j ${esc(c.wd)}</td></tr>`).join('')}</tbody></table>`;
    S.fcHtml = box.innerHTML;
    S.fcName = lok.desa || S.wil?.byCode[used]?.n || '';
    if ($('#fc-aside')) $('#fc-aside').textContent = S.fcName;
  } catch (e) { box.innerHTML = `<span style="color:var(--bahaya)">${esc(e.message)}</span>`; }
}

async function toggleWeather(k, on) {
  S.layerOn[k] = on;
  renderLegend();
  if (k === 'alerts') { drawAlerts(); return; }
  if (k === 'quake') { if (on) drawQuake(); else { if (S.layers.quake) S.map.removeLayer(S.layers.quake); if (S.layers.qsel) S.map.removeLayer(S.layers.qsel); } return; }
  if (S.layers[k]) { S.map.removeLayer(S.layers[k]); S.layers[k] = null; }
  if (!on) return;
  try {
    if (k === 'radar') {
      const m = await api('/api/weather/radar');
      const last = (m.radar?.past || []).at(-1);
      if (!last) throw new Error('Data radar tidak tersedia.');
      S.layers.radar = L.tileLayer(`${m.host}${last.path}/256/{z}/{x}/{y}/2/1_1.png`, { opacity: .65, maxNativeZoom: 7, maxZoom: 19, attribution: 'Radar &copy; RainViewer' }).addTo(S.map);
      toast(`Radar RainViewer ${fmtTime(new Date(last.time * 1000).toISOString())}. Cakupan radar di Sumatera dapat terbatas.`);
    }
    if (k === 'wind') {
      const r = await api('/api/weather/wind');
      const g = L.layerGroup();
      for (const p of r.points) {
        if (p.wd === null || p.wd === undefined) continue;
        const color = p.ws >= 40 ? '#c81e1e' : p.ws >= 25 ? '#d97706' : '#0a4f86';
        const icon = L.divIcon({ className: '', iconSize: [26, 26], html: `<svg width="26" height="26" viewBox="0 0 24 24" style="transform:rotate(${(p.wd + 180) % 360}deg)"><path d="M12 2 18 14h-4.5v8h-3v-8H6z" fill="${color}" stroke="#fff" stroke-width="1"/></svg>` });
        L.marker([p.lat, p.lon], { icon, keyboard: false }).bindTooltip(`Angin ${esc(p.ws)} km/j dari ${esc(p.wd)}°, hembusan ${esc(p.wg)} km/j<br>Hujan ${esc(p.rain)} mm, CAPE ${esc(p.cape)} J/kg<br><span style="color:#666">${esc(r.cape_note)}</span>`).addTo(g);
      }
      S.layers.wind = g.addTo(S.map);
      toast(r.direction_note);
    }
    if (k === 'aqi') {
      const r = await api('/api/weather/aqi');
      const g = L.layerGroup();
      const col = (v) => v === null ? '#e5e7eb' : v <= 50 ? '#86efac' : v <= 100 ? '#fde047' : v <= 150 ? '#fdba74' : v <= 200 ? '#f87171' : v <= 300 ? '#c084fc' : '#9f1239';
      for (const p of r.points) {
        const icon = L.divIcon({ className: '', iconSize: [40, 18], html: `<span class="aqi-badge" style="background:${col(p.us_aqi)}">${p.us_aqi ?? '?'}</span>` });
        L.marker([p.lat, p.lon], { icon }).bindTooltip(`${esc(p.name)}<br>US AQI ${esc(p.us_aqi)}, PM2.5 ${esc(p.pm2_5)} µg/m³, PM10 ${esc(p.pm10)} µg/m³<br><span style="color:#666">${esc(r.note)}</span>`).addTo(g);
      }
      S.layers.aqi = g.addTo(S.map);
    }
    if (k === 'tio_rain') {
      const r = S.tio = await api('/api/weather/tio');
      if (!r.tiles?.count) throw new Error('Tile hujan Tomorrow.io belum tersedia. ' + (r.error || 'Coba lagi beberapa menit lagi.'));
      buildTioRain(r);
      toast(`Intensitas hujan Tomorrow.io ${fmtTime(r.tiles.time)}. Ini estimasi model/nowcast, bukan radar BMKG.`);
    }
    if (k === 'tio_pts') {
      const r = S.tio = await api('/api/weather/tio');
      drawTioPoints(r);
      toast(r.direction_note);
    }
  } catch (e) { fail(e); S.layerOn[k] = false; renderLeft(); }
}

/* ---------------------------------------------------------- Tomorrow.io */
const hoursFrom = (iso) => Math.max(0, Math.round((new Date(iso) - Date.now()) / 3600000));
function etaText(p) {
  if (p.eta_heavy) return { cls: 'err', t: hoursFrom(p.eta_heavy) ? `Hujan lebat ±${hoursFrom(p.eta_heavy)} jam lagi` : 'Hujan lebat jam ini' };
  if (p.eta_rain) return { cls: 'warn', t: hoursFrom(p.eta_rain) ? `Hujan ±${hoursFrom(p.eta_rain)} jam lagi` : 'Hujan jam ini' };
  return { cls: 'ok', t: 'Tidak ada hujan berarti' };
}

function tioTooltip(p, r) {
  const e = etaText(p);
  const rows = (p.outlook || []).map(o => `<tr><td>${fmtTime(o.time, { date: false })}</td><td>${esc(o.desc)}</td><td style="text-align:right">${esc(o.prob)}%</td><td style="text-align:right">${esc(o.rain)} mm/j</td></tr>`).join('');
  return `<b>${esc(p.name)}</b><br>${esc(p.desc)}, ${esc(p.temp)}°C, lembap ${esc(p.hum)}%<br>
    Angin ${esc(p.ws)} km/j dari ${esc(p.wd)}°, hembusan ${esc(p.wg)} km/j<br><b>${esc(e.t)}</b>
    <table style="margin-top:4px;font-size:11px">${rows}</table><span style="color:#666">${esc(r.note)}</span>`;
}

function drawTioPoints(r) {
  if (S.layers.tio_pts) S.map.removeLayer(S.layers.tio_pts);
  const g = L.layerGroup();
  for (const p of r.points || []) {
    if (p.wd === null || p.wd === undefined) continue;
    const color = p.wg >= 45 || p.ws >= 40 ? '#c81e1e' : p.wg >= 30 || p.ws >= 25 ? '#d97706' : '#7c3aed';
    const ring = p.eta_heavy ? '#c81e1e' : p.eta_rain ? '#d97706' : 'transparent';
    const icon = L.divIcon({ className: '', iconSize: [30, 30], html: `<div style="width:30px;height:30px;border-radius:50%;border:2px solid ${ring};background:rgba(255,255,255,.75);display:flex;align-items:center;justify-content:center"><svg width="22" height="22" viewBox="0 0 24 24" style="transform:rotate(${(p.wd + 180) % 360}deg)"><path d="M12 2 18 14h-4.5v8h-3v-8H6z" fill="${color}" stroke="#fff" stroke-width="1"/></svg></div>` });
    L.marker([p.lat, p.lon], { icon, keyboard: false }).bindTooltip(tioTooltip(p, r), { maxWidth: 320 }).addTo(g);
  }
  S.layers.tio_pts = g;
  if (S.layerOn.tio_pts) g.addTo(S.map);
}

// Dua lapisan tile: zoom 5 seluruh Sumatra (zoom out / di luar Sumbar) dan zoom 7 detail Sumbar.
function buildTioRain(r) {
  if (S.layers.tio_rain) S.map.removeLayer(S.layers.tio_rain);
  if (S.tioRainMove) S.map.off('moveend zoomend', S.tioRainMove);
  const v = encodeURIComponent(r.tiles.time || '');
  const mk = (t) => L.tileLayer(`/api/weather/tio/tile/{z}/{x}/{y}.png?v=${v}`, { opacity: .6, minNativeZoom: t.zoom, maxNativeZoom: t.zoom,
    maxZoom: 19, bounds: t.bounds, attribution: 'Hujan &copy; Tomorrow.io' });
  const layers = (r.tiles.layers || [{ zoom: r.tiles.zoom, bounds: r.tiles.detail_bounds }]).map(t => ({ ...t, layer: mk(t) }));
  const group = L.layerGroup();
  const detail = layers.find(t => t.zoom === r.tiles.zoom), coarse = layers.find(t => t.zoom !== r.tiles.zoom) || detail;
  const sumbar = L.latLngBounds(r.tiles.detail_bounds);
  // Lapisan hanya dipakai bila tile-nya sudah lengkap di server (bisa tertunda karena kuota).
  const full = (t) => t.available === undefined || t.available >= t.count;
  const pick = () => {
    const wantDetail = S.map.getZoom() >= detail.zoom && sumbar.pad(-0.05).contains(S.map.getCenter());
    let use = wantDetail ? detail : coarse;
    if (!full(use)) { const other = use === detail ? coarse : detail; if (full(other) || (other.available || 0) > (use.available || 0)) use = other; }
    const want = use.layer;
    for (const t of layers) if (t.layer !== want && group.hasLayer(t.layer)) group.removeLayer(t.layer);
    if (!group.hasLayer(want)) group.addLayer(want);
  };
  S.tioRainMove = pick;
  S.map.on('moveend zoomend', pick);
  pick();
  S.layers.tio_rain = group.addTo(S.map);
}

async function refreshTio() {
  if (!S.cfg?.settings?.tomorrow?.enabled) return;
  try { S.tio = await api('/api/weather/tio'); } catch (e) { S.tio = { error: e.message, points: [] }; }
  renderTio();
  if (S.layerOn.tio_pts && S.tio.points?.length) drawTioPoints(S.tio);
  if (S.layerOn.tio_rain && S.tio.tiles?.count) buildTioRain(S.tio);
}

function renderTio() {
  const box = $('#tio-box');
  if (!box) return;
  const r = S.tio;
  if (!r) { box.textContent = 'Memuat…'; return; }
  if (!r.points?.length) { box.innerHTML = `<span class="muted">${esc(r.error || 'Data belum tersedia.')}</span>`; return; }
  const order = (p) => p.eta_heavy ? 0 : p.eta_rain ? 1 : 2;
  const nH = r.points.filter(p => p.eta_heavy).length, nR = r.points.filter(p => p.eta_rain).length;
  if ($('#tio-aside')) $('#tio-aside').innerHTML = nH ? `<span class="chip err">${nH} hujan lebat</span>` : nR ? `<span class="chip warn">${nR} hujan</span>` : '<span class="chip ok">cerah/berawan</span>';
  const pts = [...r.points].sort((a, b) => order(a) - order(b) || (b.max_prob || 0) - (a.max_prob || 0));
  box.innerHTML = `<table class="t"><tbody>${pts.map((p, i) => {
    const e = etaText(p);
    return `<tr data-tio="${i}" style="cursor:pointer" title="Klik untuk melihat di peta"><td><b>${esc(p.name.replace(/^UP3\s+/i, ''))}</b><div class="small muted">${esc(p.desc)}, angin ${esc(p.ws)} km/j</div></td>
      <td><span class="chip ${e.cls}">${esc(e.t)}</span><div class="small muted">peluang maks ${esc(p.max_prob ?? '-')}%</div></td></tr>`;
  }).join('')}</tbody></table>
  <p class="hint">Diperbarui ${ago(r.points_at)}. ${esc(r.note)}</p>`;
  $$('[data-tio]', box).forEach(tr => tr.onclick = () => { const p = pts[+tr.dataset.tio]; S.map.setView([p.lat, p.lon], 10); });
}

/* ================================================================ incidents */
async function refreshIncidents() {
  try { S.incidents = (await api('/api/incidents')).incidents; } catch (e) { S.incidents = []; }
  drawIncidents();
  if (S.tab !== 'layers') renderLeft();
}

function drawIncidents() {
  const g = S.layers.incidents;
  g.clearLayers();
  for (const i of S.incidents) {
    const c = catOf(i.category);
    const icon = L.divIcon({ className: '', iconSize: [26, 26], iconAnchor: [13, 26], html: `<div class="pin" style="background:${c.color};${i.status === 'selesai' ? 'opacity:.55' : ''}"><i></i></div>` });
    const m = L.marker([i.lat, i.lon], { icon, title: i.title, riseOnHover: true });
    m.bindTooltip(`<b>${esc(i.title)}</b><br>${esc(c.label)}, ${STATUS_LABEL[i.status]}<br>${fmtTime(i.occurred_at)}`, { direction: 'top', offset: [0, -24] });
    m.on('click', () => { if (!S.draw && !S.pick) openIncident(i.id); });
    g.addLayer(m);
  }
}

function startPick() {
  cancelDraw();
  S.pick = true;
  S.map.getContainer().style.cursor = 'crosshair';
  showDrawbar(`<span>Klik lokasi kejadian pada peta.</span><button class="btn btn-ghost btn-sm" id="pk-cancel">Batal</button>`);
  $('#pk-cancel').onclick = endPick;
}
function endPick() { S.pick = null; S.map.getContainer().style.cursor = ''; hideDrawbar(); }
function onMapClick(e) {
  if (S.pick) {
    const cb = typeof S.pick === 'function' ? S.pick : null;
    endPick();
    if (cb) cb(e.latlng); else incidentForm(null, e.latlng);
    return;
  }
  if (S.draw) addVertex(e.latlng);
}

function incidentForm(inc, latlng) {
  const d = inc || { title: '', category: S.cfg.categories[0]?.code, severity: 'sedang', status: 'dilaporkan', description: '', pic: S.user?.display_name || '', notes: '', visibility: 'internal', occurred_at: new Date().toISOString(), lat: latlng.lat, lon: latlng.lng };
  const opt = (arr, val) => arr.map(([k, v]) => `<option value="${esc(k)}" ${k === val ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const back = sheet({
    title: inc ? 'Ubah kejadian' : 'Kejadian baru',
    body: `<form id="f-inc">
      <label class="f"><span>Judul</span><input type="text" name="title" value="${esc(d.title)}" required minlength="3" maxlength="200" placeholder="mis. Pohon tumbang menimpa JTM F. Kuranji"></label>
      <div class="grid2">
        <label class="f"><span>Kategori</span><select name="category">${opt(S.cfg.categories.map(c => [c.code, c.label]), d.category)}</select></label>
        <label class="f"><span>Waktu kejadian (WIB)</span><input type="datetime-local" name="occurred_at" value="${toLocalInput(d.occurred_at)}" required></label>
        <label class="f"><span>Tingkat dampak</span><select name="severity">${opt(S.cfg.severities.map(s => [s, s[0].toUpperCase() + s.slice(1)]), d.severity)}</select></label>
        <label class="f"><span>Status</span><select name="status">${opt(Object.entries(STATUS_LABEL), d.status)}</select></label>
        <label class="f"><span>Lintang</span><input type="number" step="0.000001" name="lat" value="${(+d.lat).toFixed(6)}" required></label>
        <label class="f"><span>Bujur</span><input type="number" step="0.000001" name="lon" value="${(+d.lon).toFixed(6)}" required></label>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" id="inc-repick" style="margin:-4px 0 10px">Pilih ulang lokasi di peta</button>
      <label class="f"><span>Deskripsi</span><textarea name="description" maxlength="5000" placeholder="Apa yang terjadi, aset yang terdampak, kondisi akses">${esc(d.description)}</textarea></label>
      <div class="grid2">
        <label class="f"><span>Penanggung jawab</span><input type="text" name="pic" value="${esc(d.pic)}" maxlength="200"></label>
        <label class="f"><span>Tampil untuk tamu</span><select name="visibility">${opt([['internal', 'Tidak, internal saja'], ['guest', 'Ya, publikasikan']], d.visibility)}</select></label>
      </div>
      <label class="f"><span>Catatan penanganan</span><textarea name="notes" maxlength="5000">${esc(d.notes)}</textarea></label>
      <div class="notice err" id="inc-err" hidden></div>
    </form>`,
    foot: `<button class="btn btn-ghost" data-close2>Batal</button><button class="btn" id="inc-save">${inc ? 'Simpan perubahan' : 'Simpan kejadian'}</button>`,
  });
  const marker = L.marker([d.lat, d.lon], { draggable: true }).addTo(S.layers.draw);
  const form = $('#f-inc', back);
  marker.on('dragend', () => { const p = marker.getLatLng(); form.lat.value = p.lat.toFixed(6); form.lon.value = p.lng.toFixed(6); });
  const oldClose = back._close;
  back._close = () => { S.layers.draw.removeLayer(marker); oldClose(); };
  $('[data-close2]', back).onclick = back._close;
  $('[data-close]', back).onclick = back._close;
  $('#inc-repick', back).onclick = () => {
    back.style.display = 'none';
    S.pick = (ll) => { back.style.display = ''; form.lat.value = ll.lat.toFixed(6); form.lon.value = ll.lng.toFixed(6); marker.setLatLng(ll); };
    S.map.getContainer().style.cursor = 'crosshair';
    showDrawbar(`<span>Klik lokasi baru pada peta.</span><button class="btn btn-ghost btn-sm" id="pk-cancel">Batal</button>`);
    $('#pk-cancel').onclick = () => { endPick(); back.style.display = ''; };
  };
  $('#inc-save', back).onclick = async () => {
    if (!form.reportValidity()) return;
    const fd = new FormData(form);
    const body = Object.fromEntries(fd.entries());
    body.occurred_at = fromLocalInput(body.occurred_at);
    body.lat = +body.lat; body.lon = +body.lon;
    if (inc) body.revision = inc.revision;
    try {
      const r = await api(inc ? `/api/incidents/${inc.id}` : '/api/incidents', { method: inc ? 'PUT' : 'POST', body });
      toast(inc ? 'Perubahan kejadian disimpan.' : 'Kejadian disimpan.', 'ok');
      back._close();
      await refreshIncidents();
      openIncident(r.id);
    } catch (e) { const el = $('#inc-err', back); el.hidden = false; el.textContent = e.message; }
  };
}

async function openIncident(id) {
  let i;
  try { i = await api(`/api/incidents/${id}`); } catch (e) { return fail(e); }
  S.map.setView([i.lat, i.lon], Math.max(S.map.getZoom(), 14));
  const c = catOf(i.category);
  const editable = can('user');
  const photos = i.photos.map(p => `<figure><img src="/api/photos/${p.id}/thumb" alt="${esc(p.caption || 'Foto kejadian')}" data-full="/api/photos/${p.id}/file" loading="lazy">
      <figcaption>${esc(p.caption || 'Tanpa keterangan')}<br>${p.taken_at ? 'Diambil ' + fmtTime(p.taken_at) : 'Diunggah ' + fmtTime(p.uploaded_at)}${p.visibility === 'guest' ? ', publik' : ''}
      ${editable ? `<br><button class="btn btn-ghost btn-sm" data-photo-edit="${p.id}" style="margin-top:3px">Ubah</button>` : ''}</figcaption></figure>`).join('');
  const areas = i.areas.map(a => `<div class="item" style="--c:${esc(a.color)}" data-iarea="${a.id}"><h3>${esc(a.name)}</h3><div class="meta"><span class="num">${num(Math.round(a.area_km2 * 100) / 100)} km²</span><span>rev ${a.revision}</span>${a.visibility === 'guest' ? '<span>publik</span>' : ''}</div></div>`).join('');
  const analyses = i.analyses.map(a => `<div class="item" data-analysis="${a.id}"><h3>Rekap #${a.id}: ${num(a.summary.trafo_count)} gardu, ${num(a.summary.known_customer_count)} pelanggan diketahui</h3>
      <div class="meta"><span>${fmtTime(a.computed_at)}</span>${a.computed_by ? `<span>${esc(a.computed_by)}</span>` : ''}<span>${a.summary.customer_total_complete ? 'data pelanggan lengkap' : 'data pelanggan sebagian'}</span>${a.published_guest ? '<span class="chip">publik</span>' : ''}</div></div>`).join('');
  const back = sheet({
    title: i.title,
    body: `<div class="row wrap" style="margin-bottom:10px"><span class="chip" style="background:${c.color};color:#fff">${esc(c.label)}</span>
        <span class="chip" style="background:${SEV_COLOR[i.severity]};color:#fff">Dampak ${esc(i.severity)}</span>
        <span class="chip ${i.status === 'selesai' ? 'ok' : ''}">${STATUS_LABEL[i.status]}</span>${i.visibility === 'guest' ? '<span class="chip grey">Tampil untuk tamu</span>' : ''}</div>
      <dl class="kv"><dt>Waktu kejadian</dt><dd>${fmtTime(i.occurred_at, { year: true })}</dd>
        <dt>Koordinat</dt><dd class="num">${i.lat.toFixed(6)}, ${i.lon.toFixed(6)}</dd>
        ${i.pic !== undefined ? `<dt>Penanggung jawab</dt><dd>${esc(i.pic || '—')}</dd>` : ''}
        ${i.created_by ? `<dt>Dicatat</dt><dd>${esc(i.created_by)}, ${fmtTime(i.created_at)}</dd><dt>Perubahan terakhir</dt><dd>${esc(i.updated_by)}, ${fmtTime(i.updated_at)} (revisi ${i.revision})</dd>` : ''}</dl>
      ${i.description ? `<p style="white-space:pre-wrap">${esc(i.description)}</p>` : ''}
      ${i.notes ? `<div class="notice"><b>Catatan penanganan</b><div style="white-space:pre-wrap">${esc(i.notes)}</div></div>` : ''}
      <div class="sec" style="margin-top:16px"><h2>Foto dokumentasi <span class="aside">${i.photos.length}</span></h2>
        ${editable ? `<form id="f-photo" class="notice" style="margin-top:0"><div class="row wrap"><input type="file" name="file" accept="image/jpeg,image/png,image/webp" required class="grow">
          <input type="text" name="caption" placeholder="Keterangan foto" maxlength="500" class="grow">
          <select name="visibility" style="width:auto"><option value="internal">Internal</option><option value="guest">Publik</option></select>
          <button class="btn btn-sm">Unggah foto</button></div><div class="hint">JPEG, PNG atau WebP, maksimal ${S.cfg.max_photo_mb} MB.</div></form>` : ''}
        <div class="gallery">${photos || '<div class="muted small">Belum ada foto.</div>'}</div></div>
      <div class="sec"><h2>Area dampak <span class="aside">${i.areas.length}</span></h2><div class="list">${areas || '<div class="muted small">Belum ada area.</div>'}</div>
        ${editable ? `<div class="row" style="margin-top:8px"><button class="btn btn-ghost btn-sm" id="inc-draw">Gambar area untuk kejadian ini</button>${i.areas.length ? '<button class="btn btn-sm" id="inc-analyze">Hitung rekap semua area</button>' : ''}</div>` : ''}</div>
      <div class="sec"><h2>Snapshot rekap</h2><div class="list">${analyses || '<div class="muted small">Belum ada snapshot rekap tersimpan.</div>'}</div></div>`,
    foot: editable ? `<button class="btn btn-ghost" id="inc-hist">Riwayat revisi</button><button class="btn btn-ghost" id="inc-arch">Arsipkan</button><button class="btn" id="inc-edit">Ubah kejadian</button>` : '',
  });
  $$('img[data-full]', back).forEach(img => img.onclick = () => lightbox(img.dataset.full));
  $$('[data-iarea]', back).forEach(el => el.onclick = () => { const a = i.areas.find(x => x.id === +el.dataset.iarea); S.map.fitBounds(L.geoJSON(a.geometry).getBounds(), { padding: [40, 40] }); });
  $$('[data-analysis]', back).forEach(el => el.onclick = () => openSavedAnalysis(+el.dataset.analysis));
  if (!editable) return;
  $('#inc-edit', back).onclick = () => incidentForm(i);
  $('#inc-hist', back).onclick = () => openRevisions(i);
  $('#inc-arch', back).onclick = async () => {
    if (!confirm('Arsipkan kejadian ini? Data, foto dan snapshot tetap tersimpan dan dapat dipulihkan Admin.')) return;
    try { await api(`/api/incidents/${i.id}/archive`, { method: 'POST', body: { archived: true } }); toast('Kejadian diarsipkan.', 'ok'); closeSheet(); refreshIncidents(); } catch (e) { fail(e); }
  };
  $('#inc-draw', back).onclick = () => { closeSheet(); startDraw({ incident_id: i.id }); };
  if ($('#inc-analyze', back)) $('#inc-analyze', back).onclick = () => runAnalysis(i.areas.map(a => a.id), i.id);
  $$('[data-photo-edit]', back).forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    const p = i.photos.find(x => x.id === +b.dataset.photoEdit);
    const cap = prompt('Keterangan foto:', p.caption || '');
    if (cap === null) return;
    const pub = confirm('Tampilkan foto ini untuk tamu? OK = publik, Batal = internal.');
    try { await api(`/api/photos/${p.id}`, { method: 'PUT', body: { caption: cap, visibility: pub ? 'guest' : 'internal' } }); openIncident(i.id); } catch (x) { fail(x); }
  });
  $('#f-photo', back).onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const file = f.file.files[0];
    if (file && file.size > S.cfg.max_photo_mb * 1048576) return toast(`Ukuran foto melebihi ${S.cfg.max_photo_mb} MB.`, 'err');
    const btn = $('button', f); btn.disabled = true; btn.textContent = 'Mengunggah…';
    try { await api(`/api/incidents/${i.id}/photos`, { method: 'POST', form: new FormData(f) }); toast('Foto diunggah.', 'ok'); openIncident(i.id); refreshIncidents(); }
    catch (x) { fail(x); btn.disabled = false; btn.textContent = 'Unggah foto'; }
  };
}

function lightbox(src) {
  const el = document.createElement('div');
  el.className = 'lightbox';
  el.innerHTML = `<img src="${esc(src)}" alt="Foto kejadian">`;
  el.onclick = () => el.remove();
  document.body.appendChild(el);
}

async function openRevisions(i) {
  try {
    const r = await api(`/api/incidents/${i.id}/revisions`);
    sheet({
      title: `Riwayat revisi: ${i.title}`,
      body: r.revisions.map(v => `<div class="item" style="cursor:default"><h3>Revisi ${v.revision}, ${esc(v.edited_by)}</h3><div class="meta"><span>${fmtTime(v.edited_at, { year: true })}</span><span>Status ${esc(v.data.status)}</span><span>Dampak ${esc(v.data.severity)}</span></div>
        <div class="small" style="margin-top:4px">${esc(v.data.title)}. ${esc((v.data.description || '').slice(0, 200))}</div></div>`).join(''),
    });
  } catch (e) { fail(e); }
}

/* ================================================================ areas + drawing */
async function refreshAreas() {
  try { S.areas = (await api('/api/areas')).areas; } catch { S.areas = []; }
  for (const id of [...S.analysisSel]) if (!S.areas.some(a => a.id === id)) S.analysisSel.delete(id);
  drawAreas();
  if (S.tab === 'areas') renderLeft();
}

function drawAreas() {
  const g = S.layers.areas;
  g.clearLayers();
  for (const a of S.areas) {
    const sel = S.analysisSel.has(a.id);
    const l = L.geoJSON(a.geometry, { style: { color: a.color, weight: sel ? 3.5 : 2, fillColor: a.color, fillOpacity: sel ? .25 : .12 } });
    l.bindTooltip(esc(a.name), { sticky: true });
    l.on('click', (e) => { if (S.draw || S.pick) return; L.DomEvent.stop(e); openArea(a); });
    g.addLayer(l);
  }
}

function openArea(a) {
  const inc = S.incidents.find(i => i.id === a.incident_id);
  const back = sheet({
    title: a.name,
    body: `<dl class="kv"><dt>Luas</dt><dd class="num">${num(Math.round(a.area_km2 * 100) / 100)} km²</dd>
      <dt>Kejadian</dt><dd>${inc ? esc(inc.title) : a.incident_id ? '#' + a.incident_id : 'Tidak dikaitkan'}</dd>
      <dt>Revisi</dt><dd>${a.revision}${a.updated_by ? `, ${esc(a.updated_by)} ${fmtTime(a.updated_at)}` : ''}</dd>
      <dt>Tampil untuk tamu</dt><dd>${a.visibility === 'guest' ? 'Ya' : 'Tidak'}</dd></dl>`,
    foot: can('user') ? `<button class="btn btn-ghost" id="ar-arch">Arsipkan</button><button class="btn btn-ghost" id="ar-edit">Ubah bentuk</button><button class="btn btn-ghost" id="ar-meta">Ubah data</button><button class="btn" id="ar-an">Hitung rekap</button>` : '',
  });
  if (!can('user')) return;
  $('#ar-an', back).onclick = () => runAnalysis([a.id], a.incident_id);
  $('#ar-meta', back).onclick = () => areaForm(a, a.geometry);
  $('#ar-edit', back).onclick = () => {
    if (a.geometry.type !== 'Polygon') return toast('Bentuk MultiPolygon tidak dapat diedit titik demi titik. Gambar ulang area.', 'err');
    closeSheet();
    startDraw({ area: a, ring: a.geometry.coordinates[0].slice(0, -1).map(([x, y]) => L.latLng(y, x)) });
  };
  $('#ar-arch', back).onclick = async () => {
    if (!confirm('Arsipkan area ini? Snapshot rekap yang sudah memakai area ini tetap tersimpan.')) return;
    try { await api(`/api/areas/${a.id}/archive`, { method: 'POST', body: {} }); closeSheet(); refreshAreas(); toast('Area diarsipkan.', 'ok'); } catch (e) { fail(e); }
  };
}

function showDrawbar(html) { const b = $('#drawbar'); b.innerHTML = html; b.hidden = false; }
function hideDrawbar() { $('#drawbar').hidden = true; }

function startDraw(opts) {
  endPick();
  cancelDraw();
  S.draw = { pts: [], markers: [], opts };
  S.map.doubleClickZoom.disable();
  S.map.getContainer().style.cursor = 'crosshair';
  S.draw.shape = L.polygon([], { color: '#0a6ebd', weight: 2, dashArray: '4 3', fillOpacity: .15, interactive: false }).addTo(S.layers.draw);
  (opts.ring || []).forEach(addVertex);
  showDrawbar(`<span class="small" id="dw-info">Klik peta untuk menambah titik sudut. Seret titik putih untuk mengubah.</span>
    <button class="btn btn-ghost btn-sm" id="dw-undo">Urungkan titik</button><button class="btn btn-ghost btn-sm" id="dw-cancel">Batal</button>
    <button class="btn btn-sm" id="dw-done">Selesai</button>`);
  $('#dw-undo').onclick = () => { const m = S.draw.markers.pop(); if (m) S.layers.draw.removeLayer(m); S.draw.pts.pop(); updateDraw(); };
  $('#dw-cancel').onclick = cancelDraw;
  $('#dw-done').onclick = finishDraw;
  document.addEventListener('keydown', drawKeys);
}
function drawKeys(e) { if (!S.draw) return; if (e.key === 'Escape') cancelDraw(); if (e.key === 'Enter') finishDraw(); }
function addVertex(ll) {
  const d = S.draw;
  const idx = d.pts.length;
  d.pts.push(ll);
  const m = L.marker(ll, { draggable: true, icon: L.divIcon({ className: '', html: '<div class="vtx"></div>', iconSize: [12, 12] }) }).addTo(S.layers.draw);
  m.on('drag', () => { d.pts[d.markers.indexOf(m)] = m.getLatLng(); updateDraw(); });
  m.on('click', (e) => { L.DomEvent.stop(e); if (d.markers.indexOf(m) === 0 && d.pts.length >= 3) finishDraw(); });
  d.markers.push(m);
  void idx;
  updateDraw();
}
function updateDraw() {
  const d = S.draw;
  d.shape.setLatLngs(d.pts);
  const info = $('#dw-info');
  if (info) info.textContent = d.pts.length < 3 ? `Klik peta untuk menambah titik sudut (${d.pts.length}/3 minimal).` : `${d.pts.length} titik. Klik ganda, klik titik pertama, atau tekan Selesai.`;
}
function cancelDraw() {
  if (!S.draw) return;
  S.layers.draw.clearLayers();
  S.draw = null;
  S.map.doubleClickZoom.enable();
  S.map.getContainer().style.cursor = '';
  document.removeEventListener('keydown', drawKeys);
  hideDrawbar();
}
function finishDraw() {
  const d = S.draw;
  if (!d) return;
  // a dblclick also fires a click that adds a duplicate vertex
  const pts = d.pts.filter((p, i, a) => i === 0 || p.distanceTo(a[i - 1]) > 0.5);
  if (pts.length < 3) return toast('Polygon membutuhkan minimal tiga titik sudut.', 'err');
  const ring = pts.map(p => [+p.lng.toFixed(7), +p.lat.toFixed(7)]);
  ring.push([...ring[0]]);
  const geometry = { type: 'Polygon', coordinates: [ring] };
  const opts = d.opts;
  cancelDraw();
  areaForm(opts.area || null, geometry, opts.incident_id);
}

function areaForm(area, geometry, incidentId) {
  const tmp = L.geoJSON(geometry, { style: { color: '#0a6ebd', weight: 2, fillOpacity: .2 } }).addTo(S.layers.draw);
  const a = area || { name: '', color: '#0ea5e9', visibility: 'internal', incident_id: incidentId || null };
  const incOpts = S.incidents.map(i => `<option value="${i.id}" ${i.id === a.incident_id ? 'selected' : ''}>${esc(i.title)} (${fmtTime(i.occurred_at, { date: true })})</option>`).join('');
  const back = sheet({
    title: area ? 'Simpan perubahan area' : 'Area dampak baru',
    body: `<form id="f-area">
      <label class="f"><span>Nama area</span><input type="text" name="name" value="${esc(a.name)}" required minlength="2" maxlength="200" placeholder="mis. Genangan Kec. Koto Tangah"></label>
      <label class="f"><span>Kaitkan dengan kejadian</span><select name="incident_id"><option value="">Tidak dikaitkan</option>${incOpts}</select></label>
      <div class="grid2"><label class="f"><span>Warna</span><input type="color" name="color" value="${esc(a.color)}" style="height:36px;padding:2px"></label>
        <label class="f"><span>Tampil untuk tamu</span><select name="visibility"><option value="internal">Tidak</option><option value="guest" ${a.visibility === 'guest' ? 'selected' : ''}>Ya</option></select></label></div>
      <div class="notice">Luas sekitar <b class="num" id="ar-km"></b>. Setelah disimpan, centang area pada tab Area &amp; rekap untuk menghitung gardu dan pelanggan.</div>
      <div class="notice err" id="ar-err" hidden></div></form>`,
    foot: `<button class="btn btn-ghost" id="ar-x">Batal</button><button class="btn btn-ghost" id="ar-preview">Hitung rekap tanpa menyimpan</button><button class="btn" id="ar-save">Simpan area</button>`,
    onClose: () => S.layers.draw.removeLayer(tmp),
  });
  $('#ar-km', back).textContent = `${num(Math.round(polyKm2(geometry.coordinates[0]) * 100) / 100)} km²`;
  $('#ar-x', back).onclick = closeSheet;
  $('#ar-preview', back).onclick = () => runAnalysis([], null, [geometry]);
  $('#ar-save', back).onclick = async () => {
    const f = $('#f-area', back);
    if (!f.reportValidity()) return;
    const body = Object.fromEntries(new FormData(f).entries());
    body.geometry = geometry;
    body.incident_id = body.incident_id ? +body.incident_id : null;
    if (area) body.revision = area.revision;
    try {
      const r = await api(area ? `/api/areas/${area.id}` : '/api/areas', { method: area ? 'PUT' : 'POST', body });
      toast(area ? 'Perubahan area disimpan.' : 'Area disimpan.', 'ok');
      closeSheet();
      S.analysisSel.add(r.id);
      await refreshAreas(); refreshIncidents();
      S.tab = 'areas'; $$('.tab').forEach(x => x.setAttribute('aria-selected', x.dataset.tab === 'areas')); renderLeft();
    } catch (e) { const el = $('#ar-err', back); el.hidden = false; el.textContent = e.message; }
  };
}
function polyKm2(ring) {
  const lat0 = ring.reduce((s, p) => s + p[1], 0) / ring.length;
  const k = Math.cos(lat0 * Math.PI / 180) * 111.32;
  let s = 0;
  for (let i = 0; i < ring.length - 1; i++) s += ring[i][0] * k * ring[i + 1][1] * 110.574 - ring[i + 1][0] * k * ring[i][1] * 110.574;
  return Math.abs(s) / 2;
}

/* ================================================================ analysis */
async function runAnalysis(areaIds, incidentId, geometries) {
  try {
    const r = await api('/api/analysis/preview', { method: 'POST', body: { area_ids: areaIds, geometries: geometries || [] } });
    showAnalysis(r, { areaIds, incidentId, unsaved: !!(geometries && geometries.length) });
  } catch (e) { fail(e); }
}
async function openSavedAnalysis(id) {
  try {
    const r = await api(`/api/analysis/${id}`);
    showAnalysis({ ...r.result, id: r.id, saved: true, published_guest: r.published_guest, computed_by: r.computed_by }, {});
  } catch (e) { fail(e); }
}

function showAnalysis(r, ctx) {
  const items = r.items || [];
  S.layers.highlight.clearLayers();
  for (const x of items) L.circleMarker([x.lat, x.lon], { renderer: S.canvas, radius: 4.5, color: '#c81e1e', weight: 1.5, opacity: .8, fill: false, interactive: false }).addTo(S.layers.highlight);
  const pct = r.trafo_count ? Math.round(100 * r.gardu_with_customer_data / r.trafo_count) : 0;
  const saved = r.saved;
  const back = sheet({
    title: saved ? `Snapshot rekap #${r.id}` : 'Rekap area (belum disimpan)', wide: true,
    body: `<div class="notice ${r.customer_total_complete ? '' : 'warn'}">${esc(r.scope_label)}. Gardu di dalam polygon adalah aset di area tersebut, bukan bukti semua pelanggannya padam.</div>
      <div class="summary">
        <div class="stat"><b>${num(r.trafo_count)}</b><span>gardu distribusi (trafo) unik di dalam area</span></div>
        <div class="stat"><b>${num(r.known_customer_count)}</b><span>pelanggan diketahui pada gardu terpilih${r.customer_snapshot_id == null ? ' (data pelanggan belum diunggah)' : r.customer_total_complete ? '' : ' (data sebagian)'}</span></div>
        <div class="stat"><b>${num(r.gardu_with_customer_data)} / ${num(r.trafo_count)}</b><span>gardu dengan data pelanggan (${pct}%), ${num(r.gardu_missing_customer_data)} belum tersedia</span></div>
        <div class="stat"><b>${num(r.capacity_kva_total)}</b><span>total kapasitas trafo, kVA</span></div>
      </div>
      <dl class="kv small"><dt>Status nyala/padam</dt><dd>${esc(r.service_state_label || 'Belum tersedia')}</dd>
        <dt>Area</dt><dd>${(r.areas || []).map(a => `${esc(a.name || 'Area')} (${num(a.trafo)} gardu, ${num(a.area_km2)} km²)`).join('; ')}</dd>
        <dt>Dasar perhitungan</dt><dd>Snapshot aset #${esc(r.asset_snapshot_id ?? '—')}, set pelanggan #${esc(r.customer_snapshot_id ?? '—')}, aturan ${esc(r.rule_version)}, ${fmtTime(r.computed_at, { year: true })}${r.computed_by ? ' oleh ' + esc(r.computed_by) : ''}</dd></dl>
      ${!r.trafo_count ? '<div class="empty">Tidak ada gardu distribusi di dalam area ini. Periksa bentuk polygon atau pastikan data aset sudah diimpor.</div>' : ''}
      ${r.feeders?.length ? `<h3>Per penyulang (${r.feeders.length})</h3><div class="tablebox"><table class="t"><thead><tr><th>Penyulang</th><th>UP3</th><th class="n">Gardu</th><th class="n">Pelanggan diketahui</th><th class="n">Belum ada data</th><th class="n">kVA</th></tr></thead><tbody>
        ${r.feeders.map(f => `<tr><td>${esc(f.feeder)}</td><td>${esc(f.up3 || '')}</td><td class="n">${num(f.trafo)}</td><td class="n">${num(f.customers_known)}</td><td class="n">${num(f.missing)}</td><td class="n">${num(Math.round(f.kva))}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${r.lines_intersecting?.length ? `<h3>Penyulang JTM yang melintasi area (${r.lines_intersecting.length})</h3><p class="small">${r.lines_intersecting.map(l => esc(l.name)).join(', ')}</p><p class="hint">Ruas yang beririsan dilaporkan terpisah dan tidak menambah jumlah gardu.</p>` : ''}
      ${r.other_assets?.length ? `<h3>Aset lain di dalam area (${r.other_assets.length})</h3><p class="small">${r.other_assets.map(o => `${esc(o.name)} (${esc(KINDS[o.kind]?.label || o.kind)})`).join(', ')}</p>` : ''}
      ${items.length ? `<h3>Daftar gardu (${num(items.length)}${r.items_truncated ? ' pertama' : ''})</h3><div class="tablebox"><table class="t"><thead><tr><th>Nama</th><th>Penyulang</th><th>ULP</th><th>LOCATION</th><th class="n">kVA</th><th class="n">Pelanggan</th></tr></thead><tbody>
        ${items.slice(0, 1500).map(x => `<tr><td>${esc(x.name)}</td><td>${esc(x.feeder || '')}</td><td>${esc(x.ulp || '')}</td><td>${esc(x.gardu_id || '')}</td><td class="n">${x.kva ? num(x.kva) : ''}</td><td class="n">${x.customers === null || x.customers === undefined ? '<span class="muted">belum ada</span>' : num(x.customers)}</td></tr>`).join('')}</tbody></table></div>` : ''}`,
    foot: (saved ? `<a class="btn btn-ghost" href="/api/analysis/${r.id}/csv">Unduh CSV</a>
        ${can('user') ? `<button class="btn btn-ghost" id="an-pub">${r.published_guest ? 'Tarik dari tampilan tamu' : 'Publikasikan ringkasan ke tamu'}</button>` : ''}`
      : can('user') && !ctx.unsaved ? `<button class="btn" id="an-save">Simpan sebagai snapshot laporan</button>` : ctx.unsaved ? '<span class="small muted">Simpan area terlebih dahulu untuk membuat snapshot laporan.</span>' : '')
      + `<button class="btn btn-ghost" data-close3>Tutup</button>`,
    onClose: () => S.layers.highlight.clearLayers(),
  });
  $('[data-close3]', back).onclick = closeSheet;
  if ($('#an-save', back)) $('#an-save', back).onclick = async () => {
    let inc = ctx.incidentId;
    if (!inc) {
      const ids = new Set(S.areas.filter(a => ctx.areaIds.includes(a.id)).map(a => a.incident_id).filter(Boolean));
      if (ids.size === 1) inc = [...ids][0];
    }
    try {
      const s = await api('/api/analysis', { method: 'POST', body: { area_ids: ctx.areaIds, incident_id: inc || null } });
      toast(`Snapshot rekap #${s.id} disimpan.`, 'ok');
      showAnalysis(s, {});
    } catch (e) { fail(e); }
  };
  if ($('#an-pub', back)) $('#an-pub', back).onclick = async () => {
    try { await api(`/api/analysis/${r.id}/publish`, { method: 'POST', body: { published: !r.published_guest } }); r.published_guest = !r.published_guest; toast(r.published_guest ? 'Ringkasan dipublikasikan ke tamu.' : 'Ringkasan ditarik dari tampilan tamu.', 'ok'); $('#an-pub', back).textContent = r.published_guest ? 'Tarik dari tampilan tamu' : 'Publikasikan ringkasan ke tamu'; } catch (e) { fail(e); }
  };
}

/* ================================================================ admin */
function openAdmin(tab) {
  if (typeof tab !== 'string') tab = 'users';
  const tabs = [['users', 'Akun pengguna'], ['assets', 'Data aset'], ['customers', 'Data pelanggan'], ['connectors', 'Konektor dan tampilan'], ['wa', 'Notifikasi WA'], ['audit', 'Jejak audit']];
  const back = sheet({ title: 'Administrasi', wide: true, body: `<div class="subtabs" role="tablist">${tabs.map(([k, v]) => `<button data-at="${k}" aria-selected="${k === tab}">${v}</button>`).join('')}</div><div id="admin-body">Memuat…</div>` });
  $$('[data-at]', back).forEach(b => b.onclick = () => { $$('[data-at]', back).forEach(x => x.setAttribute('aria-selected', x === b)); adminTab(b.dataset.at); });
  adminTab(tab);
}

async function adminTab(t) {
  const el = $('#admin-body');
  el.innerHTML = '<span class="muted">Memuat…</span>';
  try {
    if (t === 'users') await adminUsers(el);
    if (t === 'assets') await adminAssets(el);
    if (t === 'customers') await adminCustomers(el);
    if (t === 'connectors') await adminConnectors(el);
    if (t === 'wa') await adminWa(el);
    if (t === 'audit') await adminAudit(el);
  } catch (e) { el.innerHTML = `<div class="notice err">${esc(e.message)}</div>`; }
}

async function adminUsers(el) {
  const r = await api('/api/admin/users');
  el.innerHTML = `
    <form id="f-user" class="notice" style="margin-top:0"><b>Tambah akun</b>
      <div class="grid2" style="margin-top:8px">
        <label class="f"><span>Nama pengguna</span><input type="text" name="username" required pattern="[a-z0-9._\\-]{3,64}" placeholder="huruf kecil, mis. dispatcher1"></label>
        <label class="f"><span>Nama tampilan</span><input type="text" name="display_name" maxlength="100"></label>
        <label class="f"><span>Peran</span><select name="role"><option value="user">User (operasional)</option><option value="admin">Admin</option></select></label>
        <label class="f"><span>Kata sandi awal</span><input type="text" name="password" minlength="8" required></label>
        <label class="f"><span>No. HP/WA (opsional)</span><input type="text" name="phone" maxlength="30"></label>
      </div><button class="btn btn-sm">Tambah akun</button><span class="hint"> Pengguna wajib mengganti kata sandi saat login pertama.</span></form>
    <div class="tablebox" style="max-height:none"><table class="t"><thead><tr><th>Pengguna</th><th>Peran</th><th>Status</th><th>Login terakhir</th><th></th></tr></thead><tbody>
    ${r.users.map(u => `<tr><td><b>${esc(u.username)}</b><br><span class="muted">${esc(u.display_name)}${u.phone ? ', ' + esc(u.phone) : ''}</span></td>
      <td><select data-u-role="${u.id}" style="width:auto"><option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option><option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option></select></td>
      <td><label class="check"><input type="checkbox" data-u-active="${u.id}" ${u.active ? 'checked' : ''}> Aktif</label></td>
      <td>${u.last_login_at ? fmtTime(u.last_login_at) : '—'}</td>
      <td><button class="btn btn-ghost btn-sm" data-u-reset="${u.id}">Setel ulang sandi</button></td></tr>`).join('')}</tbody></table></div>`;
  $('#f-user', el).onsubmit = async (e) => {
    e.preventDefault();
    try { await api('/api/admin/users', { method: 'POST', body: Object.fromEntries(new FormData(e.target).entries()) }); toast('Akun ditambahkan.', 'ok'); adminUsers(el); } catch (x) { fail(x); }
  };
  const upd = async (id, patch) => {
    const u = r.users.find(x => x.id === id);
    try { await api(`/api/admin/users/${id}`, { method: 'PUT', body: { display_name: u.display_name, phone: u.phone, role: u.role, active: u.active, ...patch } }); toast('Akun diperbarui.', 'ok'); adminUsers(el); } catch (x) { fail(x); adminUsers(el); }
  };
  $$('[data-u-role]', el).forEach(s => s.onchange = () => upd(+s.dataset.uRole, { role: s.value }));
  $$('[data-u-active]', el).forEach(c => c.onchange = () => upd(+c.dataset.uActive, { active: c.checked }));
  $$('[data-u-reset]', el).forEach(b => b.onclick = () => { const p = prompt('Kata sandi sementara (minimal 8 karakter):'); if (p) upd(+b.dataset.uReset, { password: p }); });
}

async function adminAssets(el) {
  const [files, snaps, job] = await Promise.all([api('/api/admin/import/server-files'), api('/api/admin/snapshots'), api('/api/admin/import/status')]);
  el.innerHTML = `
    <div class="notice" style="margin-top:0">Impor mengganti salinan aset di aplikasi dengan isi berkas terbaru. Aset yang tidak ada lagi di berkas dinonaktifkan, tidak dihapus, sehingga laporan lama tetap dapat ditelusuri. Kejadian, area dan foto tidak tersentuh.</div>
    <div class="grid2">
      <div><h3>Berkas di folder server</h3>
        ${files.files.map(f => `<div class="row" style="margin-bottom:6px"><div class="grow"><b>${esc(f.name)}</b><br><span class="small muted">${num(Math.round(f.bytes / 1048576))} MB, diubah ${fmtTime(f.modified, { year: true })}</span></div><button class="btn btn-sm" data-imp="${esc(f.name)}">Impor</button></div>`).join('') || '<div class="muted small">Tidak ada .kml/.kmz di folder aplikasi atau data\\import.</div>'}</div>
      <div><h3>Unggah KML/KMZ</h3><form id="f-kml"><input type="file" name="file" accept=".kml,.kmz" required><button class="btn btn-sm" style="margin-top:8px">Unggah dan impor</button></form>
        <p class="hint">Batas ${S.cfg.max_import_mb} MB. Ekspor dari Google Earth atau ArcGIS (Layer to KML).</p></div>
    </div>
    <div id="imp-status" class="notice" ${job.state === 'idle' ? 'hidden' : ''}></div>
    <h3>Riwayat snapshot</h3><div class="tablebox"><table class="t"><thead><tr><th>#</th><th>Sumber</th><th>Selesai</th><th>Status</th><th>Ringkasan</th></tr></thead><tbody>
    ${snaps.snapshots.map(s => `<tr><td>${s.id}</td><td>${esc(s.source_name || s.source)}<br><span class="muted">${esc(s.created_by || '')}</span></td><td>${fmtTime(s.completed_at, { year: true })}</td><td>${esc(s.state)}</td>
      <td class="small">${s.counts ? `${num(s.counts.total)} aset: baru ${num(s.counts.inserted)}, diperbarui ${num(s.counts.updated)}, dinonaktifkan ${num(s.counts.deactivated)}${s.counts.problems ? `, ${s.counts.problems} catatan` : ''}` : esc(s.error || '')}</td></tr>`).join('')}</tbody></table></div>`;
  const poll = async () => {
    const j = await api('/api/admin/import/status');
    const box = $('#imp-status');
    if (!box) return;
    box.hidden = false;
    box.className = `notice ${j.state === 'failed' ? 'err' : ''}`;
    box.innerHTML = j.state === 'running' ? `Mengimpor ${esc(j.filename)}: ${esc(j.message)}`
      : j.state === 'done' ? `Impor ${esc(j.filename)} selesai: ${num(j.result.total)} aset (${Object.entries(j.result.by_kind).map(([k, v]) => `${KINDS[k]?.label || k} ${num(v)}`).join(', ')}).${j.problems?.length ? `<br><span class="small">Catatan: ${j.problems.slice(0, 5).map(esc).join('; ')}</span>` : ''}`
        : j.state === 'failed' ? `Impor gagal: ${esc(j.message)}` : '';
    if (j.state === 'running') setTimeout(poll, 1500);
    else if (j.state === 'done' && S._importWatch) { S._importWatch = false; resetAssetLayers(); adminAssets(el); }
  };
  if (job.state !== 'idle') poll();
  $$('[data-imp]', el).forEach(b => b.onclick = async () => {
    if (!confirm(`Impor ${b.dataset.imp}? Proses berjalan di server sekitar 30 detik.`)) return;
    try { await api('/api/admin/import/server', { method: 'POST', body: { name: b.dataset.imp } }); S._importWatch = true; poll(); } catch (e) { fail(e); }
  });
  $('#f-kml', el).onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('button', e.target); btn.disabled = true; btn.textContent = 'Mengunggah…';
    try { await api('/api/admin/import/kml', { method: 'POST', form: new FormData(e.target) }); S._importWatch = true; poll(); } catch (x) { fail(x); }
    btn.disabled = false; btn.textContent = 'Unggah dan impor';
  };
}

function resetAssetLayers() {
  for (const k of Object.keys(KINDS)) { if (S.layers[k]) { S.map.removeLayer(S.layers[k]); S.layers[k] = null; } }
  loadSummary().then(() => { for (const k of Object.keys(KINDS)) if (S.layerOn[k] || KINDS[k].def) toggleKind(k, true); renderLeft(); });
  refreshAlerts();
}

async function adminCustomers(el) {
  const r = await api('/api/admin/customers/sets');
  el.innerHTML = `
    <div class="notice" style="margin-top:0">KML aset tidak memuat jumlah pelanggan. Unggah tabel jumlah pelanggan per gardu (mis. dari AP2T) agar rekap polygon dapat menghitung pelanggan. Nilai kosong dianggap <b>belum tersedia</b>, bukan nol.</div>
    <p><a class="btn btn-ghost btn-sm" href="/api/admin/customers/template.csv">Unduh template berisi daftar gardu</a></p>
    <form id="f-cust" class="notice"><div class="grid2">
      <label class="f"><span>Berkas .xlsx atau .csv</span><input type="file" name="file" accept=".xlsx,.csv" required></label>
      <label class="f"><span>Tanggal berlaku data</span><input type="date" name="effective_date"></label>
      <label class="f"><span>Kolom kunci gardu (opsional)</span><input type="text" name="key_field" placeholder="otomatis: LOCATION, KODE_GARDU, ASSETNUM, NAMA"></label>
      <label class="f"><span>Kolom jumlah pelanggan (opsional)</span><input type="text" name="count_field" placeholder="otomatis: JUMLAH_PELANGGAN, PELANGGAN"></label>
    </div><button class="btn btn-sm">Unggah data pelanggan</button></form>
    <div id="cust-res"></div>
    <h3>Riwayat unggahan</h3><div class="tablebox"><table class="t"><thead><tr><th>#</th><th>Berkas</th><th>Diunggah</th><th class="n">Baris</th><th class="n">Cocok</th><th class="n">Tidak cocok</th><th>Aktif</th></tr></thead><tbody>
    ${r.sets.map(s => `<tr><td>${s.id}</td><td>${esc(s.filename)}<br><span class="muted small">kunci ${esc(s.key_field)}, jumlah ${esc(s.count_field)}</span></td><td>${fmtTime(s.uploaded_at)}<br><span class="muted">${esc(s.uploaded_by)}</span></td><td class="n">${num(s.rows_total)}</td><td class="n">${num(s.rows_matched)}</td><td class="n">${num(s.rows_unmatched)}</td><td>${s.active ? '<span class="chip ok">aktif</span>' : ''}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">Belum ada.</td></tr>'}</tbody></table></div>`;
  $('#f-cust', el).onsubmit = async (e) => {
    e.preventDefault();
    try {
      const x = await api('/api/admin/customers/upload', { method: 'POST', form: new FormData(e.target) });
      toast(`Data pelanggan diunggah: ${x.rows_matched} gardu cocok.`, 'ok');
      await adminCustomers(el);
      $('#cust-res').innerHTML = `<div class="notice ${x.rows_unmatched ? 'warn' : ''}">${num(x.rows_matched)} dari ${num(x.rows_total)} baris cocok dengan gardu (kunci ${esc(x.key_field)}).${x.unmatched_sample.length ? ` Contoh tidak cocok: ${x.unmatched_sample.map(u => esc(u.kunci)).join(', ')}.` : ''}${x.invalid_values.length ? ` Nilai tidak valid: ${x.invalid_values.length} baris.` : ''}</div>`;
      loadSummary();
    } catch (x) { fail(x); }
  };
}

async function adminConnectors(el) {
  const c = await api('/api/admin/settings');
  const kinds = Object.entries(KINDS).map(([k, d]) => `<label class="check"><input type="checkbox" name="gl" value="${k}" ${c.guest.layers.includes(k) ? 'checked' : ''}> ${esc(d.label)}</label>`).join('');
  el.innerHTML = `<form id="f-conn">
    <h3>Analisis paparan BMKG</h3>
    <label class="f" style="max-width:260px"><span>Koridor penyulang (meter)</span><input type="number" name="corridor_m" min="0" max="2000" value="${c.corridor_m}"></label>
    <h3>Tampilan tamu tanpa login</h3>
    <label class="check"><input type="checkbox" name="g_en" ${c.guest.enabled ? 'checked' : ''}> Izinkan tamu melihat peta</label>
    <label class="check"><input type="checkbox" name="g_al" ${c.guest.show_alerts ? 'checked' : ''}> Tampilkan peringatan BMKG untuk tamu</label>
    <label class="check"><input type="checkbox" name="g_in" ${c.guest.show_incidents ? 'checked' : ''}> Tampilkan kejadian yang ditandai publik</label>
    <div class="small muted" style="margin:8px 0 4px">Lapisan aset yang terlihat tamu:</div><div class="grid2">${kinds}</div>
    <h3>Sumber cuaca tambahan</h3>
    <label class="check"><input type="checkbox" name="om_en" ${c.openmeteo.enabled ? 'checked' : ''}> Open-Meteo: angin 10 m, CAPE dan kualitas udara</label>
    <label class="f" style="max-width:380px"><span>API key Open-Meteo (kosongkan untuk API gratis)</span><input type="text" name="om_key" value="${esc(c.openmeteo.api_key)}"></label>
    <p class="hint">API gratis Open-Meteo hanya untuk penggunaan non-komersial. Untuk operasional PLN gunakan langganan berbayar dan isi API key di atas.</p>
    <label class="check"><input type="checkbox" name="rv_en" ${c.rainviewer.enabled ? 'checked' : ''}> RainViewer: radar hujan 2 jam terakhir (zoom maksimal 7, cakupan radar Sumatera dapat terbatas)</label>
    <p class="hint">RainViewer gratis sejak 2026 hanya untuk penggunaan pribadi/edukasi, skema warna tunggal, zoom maksimal 7.</p>
    <h3>Tomorrow.io</h3>
    <label class="check"><input type="checkbox" name="tio_en" ${c.tomorrow.enabled ? 'checked' : ''}> Aktifkan prakiraan Tomorrow.io (angin, hujan 6 jam per UP3, intensitas hujan saat ini)</label>
    <div class="grid2">
      <label class="f"><span>API key Tomorrow.io</span><input type="text" name="tio_key" value="${esc(c.tomorrow.api_key)}" autocomplete="off"></label>
      <label class="check" style="margin-top:22px"><input type="checkbox" name="tio_tiles" ${c.tomorrow.rain_tiles !== false ? 'checked' : ''}> Ambil tile intensitas hujan (10 panggilan per pembaruan: Sumatra + detail Sumbar)</label>
      <label class="f"><span>Interval pembaruan (menit, 30–360)</span><input type="number" name="tio_int" min="30" max="360" value="${esc(c.tomorrow.interval_min)}"></label>
      <label class="f"><span>Jumlah lokasi (kantor UP3, 1–15)</span><input type="number" name="tio_pts" min="1" max="15" value="${esc(c.tomorrow.max_points)}"></label>
    </div>
    <p class="hint" id="tio-quota"></p>
    <div class="row"><button type="button" class="btn btn-ghost btn-sm" id="btn-tio-test">Uji koneksi Tomorrow.io</button><span class="small muted" id="tio-test-res"></span></div>
    <p class="hint">Paket gratis: 25 panggilan/jam dan 500/hari. Server mengambil data terjadwal dan membaginya ke semua pengguna; API key tidak dikirim ke browser. Kualitas udara dan petir Tomorrow.io termasuk paket berbayar, jadi AQI tetap dari Open-Meteo. Data Tomorrow.io adalah model, bukan pengganti peringatan resmi BMKG.</p>
    <label class="f"><span>Tautan Windy</span><input type="text" name="windy" value="${esc(c.windy.url)}"></label>
    <h3>Peta dasar</h3>
    <label class="f"><span>Daftar peta dasar (JSON)</span><textarea name="basemaps" style="min-height:140px;font-family:Consolas,monospace;font-size:12px">${esc(JSON.stringify(c.basemaps, null, 2))}</textarea></label>
    <p class="hint">Dapat diganti dengan basemap ArcGIS organisasi atau server tile internal. Format URL {z}/{x}/{y}.</p>
    <button class="btn">Simpan pengaturan</button></form>`;
  $('#f-conn', el).onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    let basemaps;
    try { basemaps = JSON.parse(f.basemaps.value); } catch { return toast('Daftar peta dasar bukan JSON yang valid.', 'err'); }
    const body = {
      corridor_m: +f.corridor_m.value, basemaps,
      guest: { enabled: f.g_en.checked, show_alerts: f.g_al.checked, show_incidents: f.g_in.checked, layers: $$('input[name=gl]:checked', f).map(x => x.value) },
      openmeteo: { enabled: f.om_en.checked, api_key: f.om_key.value.trim() },
      rainviewer: { enabled: f.rv_en.checked }, windy: { url: f.windy.value.trim() },
      tomorrow: { enabled: f.tio_en.checked, api_key: f.tio_key.value.trim(), rain_tiles: f.tio_tiles.checked,
        interval_min: +f.tio_int.value || 60, max_points: +f.tio_pts.value || 8 },
    };
    try { await api('/api/admin/settings', { method: 'PUT', body }); toast('Pengaturan disimpan. Muat ulang halaman untuk menerapkan peta dasar.', 'ok'); S.cfg = await api('/api/config'); renderLeft(); renderRight(); setTimeout(refreshTio, 15000); } catch (x) { fail(x); }
  };
  const fc = $('#f-conn', el);
  const quota = () => {
    const per = ((+fc.tio_pts.value || 8) + (fc.tio_tiles.checked ? 10 : 0)) * 60 / Math.max(30, +fc.tio_int.value || 60);
    const q = $('#tio-quota', el);
    q.textContent = `Perkiraan pemakaian ±${per.toFixed(0)} panggilan/jam, ±${(per * 24).toFixed(0)}/hari.` + (per > 22 || per * 24 > 470 ? ' Melebihi kuota gratis, tidak dapat disimpan.' : '');
    q.style.color = per > 22 || per * 24 > 470 ? 'var(--bahaya)' : '';
  };
  ['tio_pts', 'tio_int', 'tio_tiles'].forEach(n => fc[n].oninput = fc[n].onchange = quota);
  quota();
  $('#btn-tio-test', el).onclick = async () => {
    const out = $('#tio-test-res', el);
    out.textContent = 'Menguji… (simpan pengaturan dulu bila API key baru diisi)';
    try {
      const r = await api('/api/admin/tomorrow/test', { method: 'POST' });
      out.textContent = `Berhasil: ${r.site}, ${r.now.desc}, ${r.now.temp}°C, angin ${r.now.ws} km/j. Terpakai ${r.usage.hour}/jam, ${r.usage.day}/hari.`;
      out.style.color = 'var(--aman, #15803d)';
    } catch (x) { out.textContent = x.message; out.style.color = 'var(--bahaya)'; }
  };
}

async function adminWa(el) {
  const [c, n] = await Promise.all([api('/api/admin/settings'), api('/api/admin/notifications')]);
  const w = c.wa;
  el.innerHTML = `<form id="f-wa">
    <div class="notice" style="margin-top:0">Notifikasi dikirim server ke WA gateway (Node.js) melalui HTTP. Pesan masuk antrean dan dikirim ulang otomatis bila gateway sedang mati. Peringatan BMKG dan kejadian masing-masing dikirim satu kali per revisi.</div>
    <label class="check"><input type="checkbox" name="enabled" ${w.enabled ? 'checked' : ''}> Aktifkan notifikasi WA</label>
    <div class="grid2">
      <label class="f"><span>URL endpoint kirim pesan</span><input type="text" name="url" value="${esc(w.url)}" placeholder="http://127.0.0.1:3000/send-message"></label>
      <label class="f"><span>Alamat aplikasi di pesan</span><input type="text" name="app_url" value="${esc(w.app_url)}"></label>
    </div>
    <label class="f"><span>Header tambahan (JSON), mis. token</span><input type="text" name="headers" value="${esc(JSON.stringify(w.headers))}" placeholder='{"Authorization":"Bearer ..."}'></label>
    <label class="f"><span>Templat body JSON</span><input type="text" name="body_template" value="${esc(w.body_template)}"></label>
    <p class="hint">{{target}} diganti nomor/ID grup tujuan, {{message}} diganti isi pesan. Sesuaikan dengan API WA gateway Anda.</p>
    <label class="f"><span>Tujuan (satu per baris: nomor 628xx atau ID grup)</span><textarea name="targets">${esc((w.targets || []).join('\n'))}</textarea></label>
    <div class="grid2">
      <label class="check"><input type="checkbox" name="notify_bmkg" ${w.notify_bmkg ? 'checked' : ''}> Kirim peringatan BMKG yang mengenai aset</label>
      <label class="check"><input type="checkbox" name="notify_incident" ${w.notify_incident ? 'checked' : ''}> Kirim kejadian baru/perubahan status</label>
      <label class="f"><span>Dampak minimal kejadian</span><select name="incident_min_severity">${S.cfg.severities.map(s => `<option ${s === w.incident_min_severity ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
    </div>
    <div class="row"><button class="btn">Simpan</button><input type="text" id="wa-test-to" placeholder="Nomor uji 628xx" style="max-width:200px"><button type="button" class="btn btn-ghost" id="wa-test">Kirim pesan uji</button></div></form>
    <h3>Antrean terakhir</h3><div class="tablebox"><table class="t"><thead><tr><th>Waktu</th><th>Tujuan</th><th>Status</th><th>Pesan</th></tr></thead><tbody>
    ${n.notifications.map(x => `<tr><td>${fmtTime(x.created_at)}</td><td>${esc(x.target)}</td><td><span class="chip ${x.status === 'sent' ? 'ok' : x.status === 'failed' ? 'err' : 'warn'}">${esc(x.status)}</span>${x.error ? `<br><span class="small muted">${esc(x.error)}</span>` : ''}</td><td class="small" style="white-space:pre-wrap">${esc(x.message.slice(0, 160))}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">Belum ada notifikasi.</td></tr>'}</tbody></table></div>`;
  const f = $('#f-wa', el);
  f.onsubmit = async (e) => {
    e.preventDefault();
    let headers;
    try { headers = JSON.parse(f.headers.value || '{}'); } catch { return toast('Header harus JSON, mis. {"Authorization":"Bearer abc"}', 'err'); }
    const body = { wa: { enabled: f.enabled.checked, url: f.url.value.trim(), app_url: f.app_url.value.trim(), headers, body_template: f.body_template.value,
      targets: f.targets.value.split(/\n+/).map(s => s.trim()).filter(Boolean), notify_bmkg: f.notify_bmkg.checked, notify_incident: f.notify_incident.checked, incident_min_severity: f.incident_min_severity.value } };
    try { await api('/api/admin/settings', { method: 'PUT', body }); toast('Pengaturan WA disimpan.', 'ok'); adminWa(el); } catch (x) { fail(x); }
  };
  $('#wa-test', el).onclick = async () => {
    try { const r = await api('/api/admin/notifications/test', { method: 'POST', body: { target: $('#wa-test-to').value } }); toast(`Gateway menjawab HTTP ${r.status}: ${r.response.slice(0, 120)}`, r.status < 300 ? 'ok' : 'err'); } catch (x) { fail(x); }
  };
}

async function adminAudit(el) {
  const r = await api('/api/admin/audit?limit=300');
  el.innerHTML = `<div class="tablebox" style="max-height:70vh"><table class="t"><thead><tr><th>Waktu</th><th>Pelaku</th><th>Aksi</th><th>Objek</th><th>Rincian</th><th>IP</th></tr></thead><tbody>
    ${r.events.map(e => `<tr><td>${fmtTime(e.at)}</td><td>${esc(e.actor_name || '')}</td><td>${esc(e.action.replace(/_/g, ' '))}</td><td>${esc(e.entity_type || '')} ${esc(e.entity_id || '')}</td><td class="small">${esc(JSON.stringify(e.detail || {}).slice(0, 160))}</td><td>${esc(e.ip || '')}</td></tr>`).join('')}</tbody></table></div>`;
}

document.addEventListener('DOMContentLoaded', boot);
