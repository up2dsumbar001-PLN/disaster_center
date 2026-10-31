"""Capture grup WhatsApp: gateway tiruan lewat HTTP, tanpa PostgreSQL."""
import http.server
import json
import threading
import types
import urllib.parse

import pytest

from dcc import appsettings, wag

GROUP = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
IMG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FOREIGN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TOKEN = "wak_uji"


def msg(id, **kw):
    return {"id": id, "chat_id": GROUP, "kind": "text", "body": "halo", "sender_name": "BMKG", "sender_phone": "+62812",
            "from_me": False, "wa_timestamp": "2026-10-31T08:00:00.000Z", "content_state": "ok", "edited_at": None,
            "media": None, **kw}


class Gateway(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Gateway.hits.append(self.path)
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.reply(401, b"{}")
        path = urllib.parse.urlparse(self.path).path
        image = msg(IMG, kind="image", body="foto", media={"availability": "stored", "mimetype": "image/png", "path": "x",
                                                           "sha256": "h", "stored_bytes": 3, "url": f"/api/v1/messages/{IMG}/media"})
        if path == "/api/v1/chats":
            return self.data([{"id": OTHER, "type": "group", "title": "Lain"}, {"id": GROUP, "type": "group", "title": "BMKG & Co"}])
        if path == "/api/v1/messages":
            return self.data([msg("3", content_state="revoked", body=None, kind="image",
                                  media={"availability": "stored", "url": "/x"}),
                              msg("2", kind="unsupported", body=None), image, msg("1")])
        if path == f"/api/v1/messages/{IMG}":
            return self.data(image)
        if path == f"/api/v1/messages/{FOREIGN}":
            return self.data(msg(FOREIGN, chat_id=OTHER))
        if path == f"/api/v1/messages/{IMG}/media":
            return self.reply(200, b"png", "image/png")
        self.reply(404, b"{}")

    def data(self, value):
        self.reply(200, json.dumps({"data": value}).encode())

    def reply(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeDb:
    def __init__(self, conf):
        self.conf, self.health = conf, []

    def one(self, sql, args=()):
        return {"value": {"wag": self.conf}}

    def run(self, sql, args=()):
        self.health.append(args[1])
        return 1


@pytest.fixture()
def gateway():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Gateway.hits = []
    appsettings.reset_cache()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    appsettings.reset_cache()


def client(url, **kw):
    conf = {"enabled": True, "url": url, "token": TOKEN, "group": "bmkg & co", **kw}
    db = FakeDb(conf)
    appsettings.reset_cache()
    return wag.Wag(types.SimpleNamespace(db=db)), db


def test_messages_are_trimmed_ordered_and_cached(gateway):
    w, db = client(gateway)
    r = w.messages()
    assert r["configured"] and [m["id"] for m in r["messages"]] == ["1", IMG, "3"]
    text, image, gone = r["messages"]
    assert "sender_phone" not in text and text["sender"] == "BMKG"
    assert image["media"] == {"state": "stored", "mime": "image/png", "name": None, "bytes": 3, "url": f"/api/wag/media/{IMG}"}
    assert gone["revoked"] and gone["media"] is None
    assert db.health == ["ok"]
    w.messages()
    assert sum("/messages?" in h for h in Gateway.hits) == 1


def test_no_token_means_not_configured_without_calling_gateway(gateway):
    w, _ = client(gateway, token="")
    assert w.messages() == {"configured": False, "group": "bmkg & co", "messages": []}
    assert Gateway.hits == []


def test_bad_token_and_unknown_group_report_clearly(gateway):
    w, db = client(gateway, token="salah")
    with pytest.raises(wag.WagError, match="Token ditolak"):
        w.messages()
    assert db.health == ["error"]
    w, _ = client(gateway, group="tidak ada")
    with pytest.raises(wag.WagError, match="tidak ditemukan"):
        w.messages()


def test_media_only_from_configured_group(gateway):
    w, _ = client(gateway)
    assert w.media(IMG) == (b"png", "image/png", IMG)
    with pytest.raises(wag.WagNotFound):
        w.media(FOREIGN)
    assert f"/api/v1/messages/{FOREIGN}/media" not in Gateway.hits


def test_token_masked_and_kept_on_save():
    cfg = appsettings._merge(appsettings.DEFAULTS, {"wag": {"token": "rahasia"}})
    assert appsettings.masked(cfg)["wag"]["token"] == "********"
    assert "token" not in appsettings.public_view(cfg)["wag"]
