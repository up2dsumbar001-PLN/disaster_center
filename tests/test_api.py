"""End-to-end API tests against a real PostgreSQL server.

Requires a local PostgreSQL reachable as postgres@127.0.0.1 with trust auth
(set DCC_TEST_PG="host=... user=..." to override). Uses psycopg when
installed, otherwise the bundled wire-protocol shim.
"""
import http.server
import io
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from dcc import appsettings
from dcc.config import Settings
from dcc.db import Database

try:
    import psycopg  # noqa: F401
    CONNECT = None
except ImportError:
    from tests import pgwire_shim
    CONNECT = pgwire_shim.connect

PG = os.environ.get("DCC_TEST_PG", "host=127.0.0.1 port=5432 user=postgres")

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>UJI</name>
<Folder><name>ASET JARINGAN</name>
 <Folder><name>GI SUMBAR</name><Placemark><name>GI Uji</name><Point><coordinates>100.30,-0.95,0</coordinates></Point></Placemark></Folder>
 <Folder><name>Trafo Sumbar</name><Folder><name>PADANG</name><Folder><name>F UJI (3 trafo)</name>
  <Placemark><name>F UJI - G1</name><description><![CDATA[<table><tr><td>LOCATION</td><td>GD01.1</td></tr><tr><td>globalid_1</td><td>{G1}</td></tr><tr><td>KAPASITAS</td><td>100</td></tr><tr><td>DESCRIPTION</td><td>TRAFO 1 GD PDG-ABC-T-0001 SATU</td></tr></table>]]></description><Point><coordinates>100.05,-0.9,0</coordinates></Point></Placemark>
  <Placemark><name>F UJI - G2</name><description><![CDATA[<table><tr><td>LOCATION</td><td>GD01.2</td></tr><tr><td>globalid_1</td><td>{G2}</td></tr><tr><td>KAPASITAS</td><td>160</td></tr></table>]]></description><Point><coordinates>100.15,-0.9,0</coordinates></Point></Placemark>
  <Placemark><name>F UJI - G3</name><description><![CDATA[<table><tr><td>LOCATION</td><td>GD01.3</td></tr><tr><td>globalid_1</td><td>{G3}</td></tr><tr><td>KAPASITAS</td><td>&lt;Null&gt;</td></tr></table>]]></description><Point><coordinates>100.3,-0.9,0</coordinates></Point></Placemark>
 </Folder></Folder></Folder>
 <Folder><name>JTM Sumbar</name><Folder><name>JTM_Padang</name><Placemark><name>F UJI</name><MultiGeometry>
  <LineString><coordinates>99.9,-0.9,0 100.5,-0.9,0</coordinates></LineString><LineString><coordinates>100.5,-0.9,0 100.5,-0.7,0</coordinates></LineString>
 </MultiGeometry></Placemark></Folder></Folder>
</Folder>
<Folder><name>Batas_ULP_Local</name><Placemark><name>ULP Uji</name><description>ULP Uji &lt;br&gt; UP3 PADANG</description><Polygon><outerBoundaryIs><LinearRing><coordinates>99.8,-1.2 100.6,-1.2 100.6,-0.6 99.8,-0.6 99.8,-1.2</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></Folder>
</Document></kml>"""

A = {"type": "Polygon", "coordinates": [[[100.0, -1.0], [100.2, -1.0], [100.2, -0.8], [100.0, -0.8], [100.0, -1.0]]]}
B = {"type": "Polygon", "coordinates": [[[100.1, -1.0], [100.4, -1.0], [100.4, -0.8], [100.1, -0.8], [100.1, -1.0]]]}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, None

    def req(self, method, path, body=None, headers=None, raw=False, csrf=True):
        h = dict(headers or {})
        data = None
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            data = body
        if csrf:
            h["X-DCC"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            resp = urllib.request.urlopen(r, timeout=60)
            status, content, hdrs = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, content, hdrs = e.code, e.read(), e.headers
        sc = hdrs.get("set-cookie")
        if sc and "dcc_session=" in sc:
            val = sc.split(";")[0]
            self.cookie = None if val.endswith('=""') or val == "dcc_session=" else val
        if raw:
            return status, content, hdrs
        try:
            return status, json.loads(content or b"null")
        except ValueError:
            return status, content.decode("utf-8", "replace")

    def get(self, p, **k):
        return self.req("GET", p, **k)

    def post(self, p, b=None, **k):
        return self.req("POST", p, b if b is not None else {}, **k)

    def put(self, p, b, **k):
        return self.req("PUT", p, b, **k)

    def upload(self, path, files: dict, fields: dict | None = None):
        bnd = uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in (fields or {}).items():
            buf.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (name, data) in files.items():
            buf.write(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        buf.write(f"--{bnd}--\r\n".encode())
        return self.req("POST", path, buf.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})


class Hook(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        Hook.received.append({"body": json.loads(self.rfile.read(n)), "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":true}')

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import uvicorn

    from dcc.app import create_app

    dbname = "dcc_it_" + uuid.uuid4().hex[:8]
    conn = (CONNECT or __import__("psycopg").connect)(PG + " dbname=postgres")
    if CONNECT:
        conn._simple(f"CREATE DATABASE {dbname}")
    else:
        conn.autocommit = True
        conn.execute(f"CREATE DATABASE {dbname}")
    conn.close()
    appsettings.reset_cache()
    data_dir = tmp_path_factory.mktemp("data")
    st = Settings(port=free_port(), database_url=f"{PG} dbname={dbname}", secret_key="x" * 32,
                  allowed_networks=[__import__("ipaddress").ip_network("127.0.0.0/8")], data_dir=data_dir,
                  initial_admin_password="AdminAwal123", collector_enabled=False)
    db = Database(st.database_url, connect=CONNECT)
    app = create_app(st, db, start_collector=False)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=st.port, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    hook = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{st.port}", "ctx": app.state.ctx, "hook": hook, "data_dir": data_dir}
    srv.should_exit = True
    hook.shutdown()
    th.join(timeout=5)


@pytest.fixture(scope="module")
def admin(server):
    c = Client(server["base"])
    st, r = c.post("/api/auth/login", {"username": "admin", "password": "AdminAwal123"})
    assert st == 200, r
    st, r = c.get("/api/admin/users")
    assert st == 403 and r.get("must_change_password")
    st, r = c.post("/api/me/password", {"old_password": "AdminAwal123", "new_password": "RahasiaBaru#2026"})
    assert st == 200, r
    return c


def test_guest_and_csrf(server):
    g = Client(server["base"])
    st, r = g.get("/api/config")
    assert st == 200 and r["role"] == "guest"
    st, r = g.post("/api/incidents", {"title": "x"})
    assert st == 401
    st, r = g.post("/api/auth/login", {"username": "admin", "password": "salah"}, csrf=False)
    assert st == 403
    st, r = g.post("/api/auth/login", {"username": "admin", "password": "salah"})
    assert st == 401
    st, body, h = g.get("/", raw=True)
    assert st == 200 and "content-security-policy" in {k.lower() for k in h.keys()}


def test_import_and_layers(server, admin):
    st, r = admin.upload("/api/admin/import/kml", {"file": ("uji.kml", KML.encode())})
    assert st == 200, r
    for _ in range(100):
        st, job = admin.get("/api/admin/import/status")
        if job["state"] != "running":
            break
        time.sleep(0.1)
    assert job["state"] == "done", job
    assert job["result"]["by_kind"] == {"gi": 1, "trafo": 3, "jtm": 1, "ulp": 1}
    st, layer = admin.get("/api/assets/layer/trafo")
    assert st == 200 and layer["count"] == 3
    names = {row[3] for row in layer["rows"]}
    assert names == {"F UJI - G1", "F UJI - G2", "F UJI - G3"}
    g = Client(server["base"])
    st, _ = g.get("/api/assets/layer/trafo")
    assert st == 403  # trafo not in default guest layers
    st, jtm = g.get("/api/assets/layer/jtm")
    assert st == 200 and jtm["count"] == 1
    st, s = admin.get("/api/assets/search?q=g2")
    assert st == 200 and s["results"][0]["name"] == "F UJI - G2"
    # re-import is idempotent (no duplicates)
    admin.upload("/api/admin/import/kml", {"file": ("uji.kml", KML.encode())})
    for _ in range(100):
        st, job = admin.get("/api/admin/import/status")
        if job["state"] != "running":
            break
        time.sleep(0.1)
    assert job["result"]["inserted"] == 0 and job["result"]["updated"] == 6
    tid = next(r for r in server["ctx"].index.by_kind["trafo"] if r["name"] == "F UJI - G1")
    assert tid["ulp"] == "ULP Uji" and tid["gardu_id"] == "GD01.1" and tid["code"] == "PDG-ABC-T-0001"


def test_customers_incident_area_analysis(server, admin):
    csv_data = "LOCATION;JUMLAH_PELANGGAN\nGD01.1;100\nGD01.2;150\nGD01.3;\nGD99.9;5\n".encode()
    st, r = admin.upload("/api/admin/customers/upload", {"file": ("plg.csv", csv_data)})
    assert st == 200, r
    assert r["rows_matched"] == 3 and r["rows_unmatched"] == 1
    st, inc = admin.post("/api/incidents", {"title": "Banjir uji", "category": "banjir", "lon": 100.1, "lat": -0.9,
                                            "severity": "tinggi", "visibility": "guest"})
    assert st == 200, inc
    st, a = admin.post("/api/areas", {"name": "Area A", "incident_id": inc["id"], "geometry": A})
    assert st == 200, a
    st, b = admin.post("/api/areas", {"name": "Area B", "incident_id": inc["id"], "geometry": B, "visibility": "guest"})
    assert st == 200
    st, res = admin.post("/api/analysis/preview", {"area_ids": [a["id"], b["id"]]})
    assert st == 200, res
    assert (res["trafo_count"], res["known_customer_count"], res["gardu_with_customer_data"],
            res["gardu_missing_customer_data"], res["customer_total_complete"]) == (3, 250, 2, 1, False)
    assert res["lines_intersecting"] and [o["name"] for o in res["other_assets"]] == ["GI Uji"]
    st, saved = admin.post("/api/analysis", {"area_ids": [a["id"], b["id"]], "incident_id": inc["id"]})
    assert st == 200 and saved["saved"]
    st, body, _ = admin.get(f"/api/analysis/{saved['id']}/csv", raw=True)
    assert st == 200 and body.decode("utf-8-sig").count("\n") == 5
    # snapshot stays immutable after customer data changes
    admin.upload("/api/admin/customers/upload", {"file": ("plg2.csv", b"LOCATION,JUMLAH_PELANGGAN\nGD01.3,40\n")})
    st, again = admin.get(f"/api/analysis/{saved['id']}")
    assert again["result"]["known_customer_count"] == 250
    # unsaved drawn geometry preview
    st, res2 = admin.post("/api/analysis/preview", {"geometries": [A]})
    assert st == 200 and res2["trafo_count"] == 2
    # invalid polygon rejected
    st, err = admin.post("/api/areas", {"name": "Rusak", "geometry": {"type": "Polygon", "coordinates": [[[100, -1], [101, 0],
                                                                                                        [101, -1], [100, 0]]]}})
    assert st == 422 and "berpotongan" in err["error"]
    # optimistic concurrency
    st, upd = admin.put(f"/api/incidents/{inc['id']}", {**inc, "status": "diverifikasi", "revision": inc["revision"]})
    assert st == 200 and upd["revision"] == 2
    st, conflict = admin.put(f"/api/incidents/{inc['id']}", {**inc, "status": "selesai", "revision": 1})
    assert st == 409
    # guest sees incident + only guest area, analysis hidden until published
    g = Client(server["base"])
    st, gi = g.get(f"/api/incidents/{inc['id']}")
    assert st == 200 and len(gi["areas"]) == 1 and gi["analyses"] == [] and "pic" not in gi
    admin.post(f"/api/analysis/{saved['id']}/publish", {"published": True})
    st, gi = g.get(f"/api/incidents/{inc['id']}")
    assert len(gi["analyses"]) == 1
    st, ga = g.get(f"/api/analysis/{saved['id']}")
    assert st == 200 and "items" not in ga["result"]
    st, _ = g.get(f"/api/analysis/{saved['id']}/csv")
    assert st == 401
    server["inc"] = inc


def test_photos(server, admin):
    from PIL import Image

    inc = server["inc"]
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (30, 140, 220)).save(buf, "PNG")
    st, p = admin.upload(f"/api/incidents/{inc['id']}/photos", {"file": ("foto.png", buf.getvalue())},
                         {"caption": "Genangan", "visibility": "internal"})
    assert st == 200, p
    st, body, h = admin.get(f"/api/photos/{p['id']}/thumb", raw=True)
    assert st == 200 and body[:3] == b"\xff\xd8\xff"
    g = Client(server["base"])
    st, _ = g.get(f"/api/photos/{p['id']}/file")
    assert st == 404  # internal photo hidden from guest
    st, bad = admin.upload(f"/api/incidents/{inc['id']}/photos", {"file": ("x.jpg", b"not an image at all")})
    assert st == 422
    # survives "restart": file on disk + row in DB
    row = server["ctx"].db.one("SELECT object_key FROM incident_photos WHERE id=%s", (p["id"],))
    assert (Path(server["data_dir"]) / row["object_key"]).exists()


def test_bmkg_cycle_and_wa(server, admin):
    ctx = server["ctx"]
    hook_url = f"http://127.0.0.1:{server['hook'].server_address[1]}/send"
    st, cfg = admin.put("/api/admin/settings", {"wa": {"enabled": True, "url": hook_url, "targets": ["120363@g.us"],
                                                       "headers": {"Authorization": "Bearer t0ken"}}})
    assert st == 200 and cfg["wa"]["headers"]["Authorization"] == "********"
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))
    feed = ('<rss><channel><item><title>Peringatan Dini Cuaca Sumatera Barat</title>'
            '<link>https://www.bmkg.go.id/alerts/nowcast/id/UJI001_alert.xml</link></item></channel></rss>')
    capx = (f'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>UJI001</identifier><sent>{iso(now - 60)}</sent>'
            f'<status>Actual</status><msgType>Alert</msgType><info><language>id</language><headline>Hujan lebat uji</headline>'
            f'<effective>{iso(now - 60)}</effective><expires>{iso(now + 3600)}</expires><area><areaDesc>Kec. Uji</areaDesc>'
            '<polygon>-1.0,100.0 -1.0,100.2 -0.8,100.2 -0.8,100.0 -1.0,100.0</polygon></area></info></alert>')
    res = ctx.collector.run_cap_cycle(feed, {"UJI001_alert.xml": capx})
    assert res["state"] == "ok" and res["active"] == 1
    st, al = admin.get("/api/weather/alerts")
    a = al["alerts"][0]
    assert a["id"] == "UJI001" and a["exposure"]["trafo"] == 2 and a["exposure"]["jtm"] == 1
    st, lst = admin.get("/api/weather/alerts/UJI001/assets")
    assert {x["name"] for x in lst["assets"] if x["kind"] == "trafo"} == {"F UJI - G1", "F UJI - G2"}
    # cancel suppresses
    cancel = (f'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>UJI002</identifier><sent>{iso(now - 30)}</sent>'
              '<status>Actual</status><msgType>Cancel</msgType><references>bmkg,UJI001,x</references></alert>')
    feed2 = feed.replace("</channel>", '<item><title>Sumatera Barat batal</title><link>https://www.bmkg.go.id/alerts/nowcast/id/'
                                       'UJI002_alert.xml</link></item></channel>')
    # notifications queued for the first alert + high-severity incident earlier? process queue
    sent = ctx.collector.process_notifications()
    assert sent >= 1
    msgs = [r["body"]["message"] for r in Hook.received]
    assert any("PERINGATAN BMKG" in m and "2 gardu/trafo" in m for m in msgs)
    assert all(r["auth"] == "Bearer t0ken" and r["body"]["number"] == "120363@g.us" for r in Hook.received)
    res = ctx.collector.run_cap_cycle(feed2, {"UJI001_alert.xml": capx, "UJI002_alert.xml": cancel})
    st, al = admin.get("/api/weather/alerts")
    assert al["alerts"] == []
    # source failure is shown as error, not "safe"
    from dcc import collector as col
    res = ctx.collector.run_cap_cycle("<html>bukan rss</html>")
    st, al = admin.get("/api/weather/alerts")
    h = {x["source"]: x for x in al["health"]}
    assert h["bmkg_cap"]["state"] == "error"
    assert col.render_body('{"m":"{{message}}"}', "1", 'a"b\n') == b'{"m":"a\\"b\\n"}'


def test_user_admin_and_audit(server, admin):
    st, r = admin.post("/api/admin/users", {"username": "operator1", "display_name": "Operator", "role": "user",
                                            "password": "OperatorDcc1"})
    assert st == 200, r
    u = Client(server["base"])
    st, _ = u.post("/api/auth/login", {"username": "operator1", "password": "OperatorDcc1"})
    assert st == 200
    u.post("/api/me/password", {"old_password": "OperatorDcc1", "new_password": "OperatorDcc2"})
    st, _ = u.get("/api/admin/users")
    assert st == 403
    st, inc = u.post("/api/incidents", {"title": "Pohon tumbang", "category": "pohon_tumbang", "lon": 100.2, "lat": -0.9})
    assert st == 200
    st, ev = admin.get("/api/admin/audit")
    actions = [e["action"] for e in ev["events"]]
    assert "buat_kejadian" in actions and "impor_aset" in actions and "login_gagal" in actions
    me = server["ctx"].db.one("SELECT id FROM users WHERE username='admin'")
    st, err = admin.put(f"/api/admin/users/{me['id']}", {"role": "user", "active": True})
    assert st == 422  # last admin protection
    st, _ = u.post("/api/auth/logout")
    st, r = u.get("/api/me")
    assert r["role"] == "guest"


def test_ip_allowlist_unit():
    import ipaddress
    s = Settings(allowed_networks=[ipaddress.ip_network("10.17.119.0/24"), ipaddress.ip_network("127.0.0.1/32")])
    assert s.ip_allowed("10.17.119.45") and s.ip_allowed("127.0.0.1")
    assert not s.ip_allowed("10.17.120.5") and not s.ip_allowed("192.168.1.2") and not s.ip_allowed(None)
    assert s.ip_allowed("::ffff:10.17.119.9")
