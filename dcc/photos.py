"""Photo validation and storage (filesystem object store under DATA_DIR/photos)."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
from pathlib import Path

ALLOWED = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


class PhotoError(ValueError):
    pass


def _magic_ok(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def store(root: Path, data: bytes, max_mb: int) -> dict:
    if not data:
        raise PhotoError("Berkas kosong.")
    if len(data) > max_mb * 1024 * 1024:
        raise PhotoError(f"Ukuran foto melebihi {max_mb} MB.")
    if not _magic_ok(data):
        raise PhotoError("Format tidak didukung. Gunakan JPEG, PNG atau WebP.")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise PhotoError("Paket Pillow belum terpasang di server.") from exc
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in ALLOWED:
            raise PhotoError("Format tidak didukung. Gunakan JPEG, PNG atau WebP.")
        if img.width * img.height > 60_000_000:
            raise PhotoError("Resolusi foto terlalu besar.")
        taken = None
        try:
            exif = img.getexif()
            raw = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
            if raw:
                taken = dt.datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").replace(
                    tzinfo=dt.timezone(dt.timedelta(hours=7)))
        except Exception:
            taken = None
        img = ImageOps.exif_transpose(img)
        width, height = img.size
        thumb = img.copy()
        thumb.thumbnail((480, 480))
        if thumb.mode not in ("RGB", "L"):
            thumb = thumb.convert("RGB")
        tbuf = io.BytesIO()
        thumb.save(tbuf, "JPEG", quality=78, optimize=True)
    except PhotoError:
        raise
    except Exception as exc:
        raise PhotoError("Berkas bukan gambar yang valid atau rusak.") from exc

    ctype, ext = ALLOWED[fmt]
    now = dt.datetime.now()
    rel_dir = Path("photos") / f"{now:%Y}" / f"{now:%m}"
    (root / rel_dir).mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    key = (rel_dir / f"{name}{ext}").as_posix()
    tkey = (rel_dir / f"{name}_thumb.jpg").as_posix()
    (root / key).write_bytes(data)
    (root / tkey).write_bytes(tbuf.getvalue())
    return {"object_key": key, "thumb_key": tkey, "content_type": ctype, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height, "taken_at": taken}


def resolve(root: Path, key: str) -> Path:
    p = (root / key).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise PhotoError("Path tidak valid.")
    return p
