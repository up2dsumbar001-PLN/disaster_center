"""Pesan satu grup WhatsApp dari wa_gateway (hanya baca) untuk panel kanan.

Token gateway hanya berada di server. Browser mengambil pesan dan media lewat
/api/wag/*, dan nomor telepon anggota grup tidak diteruskan ke browser.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import appsettings
from .collector import UA, set_health

CACHE_SECONDS = 15  # dipakai bersama semua pengguna; gateway tidak dipanggil per browser
MAX_MESSAGES = 60
MAX_MEDIA_BYTES = 50 * 1024 * 1024
# Sama dengan daftar aman gateway; jenis lain selalu diunduh, tidak dibuka di origin aplikasi.
INLINE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf",
                "video/mp4", "audio/ogg", "audio/mpeg", "audio/mp4"}


class WagError(Exception):
    pass


class WagNotFound(WagError):
    pass


def message_view(m: dict) -> dict | None:
    """Ambil hanya yang perlu tampil; path, hash dan nomor telepon berhenti di server."""
    kind = m.get("kind")
    revoked = m.get("content_state") == "revoked"
    media = m.get("media") or {}
    if kind == "unsupported" and not revoked:
        return None
    if kind == "text" and not m.get("body") and not revoked:
        return None
    view = None
    if media.get("availability"):
        view = {"state": media["availability"], "mime": media.get("mimetype"), "name": media.get("file_name"),
                "bytes": media.get("stored_bytes") or media.get("size_bytes"),
                "url": f"/api/wag/media/{m['id']}" if media.get("url") else None}
    return {"id": m["id"], "sender": m.get("sender_name") or ("Akun gateway" if m.get("from_me") else "Anggota grup"),
            "from_me": bool(m.get("from_me")), "kind": kind, "text": m.get("body"), "at": m.get("wa_timestamp"),
            "edited": bool(m.get("edited_at")), "revoked": revoked, "media": None if revoked else view}


class Wag:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.RLock()
        self.sig = None
        self.chat_id = None
        self.cached = None
        self.cached_at = 0.0

    def cfg(self) -> dict:
        return appsettings.get(self.ctx.db)["wag"]

    @staticmethod
    def _get(cfg: dict, path: str, max_bytes: int = 4_000_000):
        req = urllib.request.Request(cfg["url"].rstrip("/") + path,
                                     headers={"User-Agent": UA, "Authorization": f"Bearer {cfg['token']}"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise WagError("Berkas melebihi batas 50 MB yang dapat ditampilkan.")
                return resp.headers, data
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise WagError("Token ditolak gateway. Periksa token dan scope read:messages.") from exc
            if exc.code == 404:
                raise WagNotFound("Pesan atau media tidak ditemukan di gateway.") from exc
            raise WagError(f"Gateway menjawab HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WagError(f"WA gateway tidak dapat dihubungi: {getattr(exc, 'reason', exc)}") from exc

    def _json(self, cfg: dict, path: str):
        _, data = self._get(cfg, path)
        try:
            return json.loads(data)["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WagError("Jawaban gateway tidak dikenali.") from exc

    def _chat(self, cfg: dict) -> str:
        with self.lock:
            sig = (cfg["url"], cfg["token"], cfg["group"])
            if sig != self.sig:
                self.sig, self.chat_id, self.cached = sig, None, None
            if self.chat_id:
                return self.chat_id
            want = cfg["group"].strip().lower()
            hits = [x for x in self._json(cfg, "/api/v1/chats")
                    if x.get("type") == "group" and (x.get("title") or "").strip().lower() == want]
            if not hits:
                raise WagError(f"Grup \"{cfg['group']}\" tidak ditemukan di gateway atau di luar cakupan token.")
            self.chat_id = hits[0]["id"]
            return self.chat_id

    def messages(self) -> dict:
        cfg = self.cfg()
        if not (cfg.get("enabled") and cfg.get("token")):
            return {"configured": False, "group": cfg["group"], "messages": []}
        with self.lock:
            try:
                chat = self._chat(cfg)
                if self.cached and time.time() - self.cached_at < CACHE_SECONDS:
                    return self.cached
                rows = self._json(cfg, f"/api/v1/messages?chat_id={urllib.parse.quote(chat)}&limit={MAX_MESSAGES}")
            except WagError as exc:
                self.chat_id = None
                set_health(self.ctx.db, "wag", "error", str(exc))
                raise
            shown = [v for v in map(message_view, reversed(rows)) if v]
            self.cached = {"configured": True, "group": cfg["group"], "messages": shown}
            self.cached_at = time.time()
            set_health(self.ctx.db, "wag", "ok")
            return self.cached

    def media(self, message_id: str) -> tuple[bytes, str, str]:
        """Isi file, tipe dan nama. Hanya media pesan dari grup yang dikonfigurasi."""
        cfg = self.cfg()
        if not (cfg.get("enabled") and cfg.get("token")):
            raise WagNotFound("Capture WhatsApp belum diaktifkan.")
        chat = self._chat(cfg)
        meta = self._json(cfg, f"/api/v1/messages/{message_id}")
        if meta.get("chat_id") != chat:
            raise WagNotFound("Pesan atau media tidak ditemukan di gateway.")
        headers, data = self._get(cfg, f"/api/v1/messages/{message_id}/media", MAX_MEDIA_BYTES)
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return data, ctype, (meta.get("media") or {}).get("file_name") or message_id
