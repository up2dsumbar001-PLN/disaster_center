"""Configuration loaded from environment variables and an optional .env file."""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "ya", "on"}


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 18085
    database_url: str = "host=127.0.0.1 port=5432 dbname=disaster_center user=dcc_app password=ganti"
    secret_key: str = ""
    allowed_networks: list = field(default_factory=list)
    trust_proxy_headers: bool = False
    data_dir: Path = BASE_DIR / "data"
    session_hours: int = 12
    cookie_secure: bool = False
    initial_admin_user: str = "admin"
    initial_admin_password: str = ""
    collector_enabled: bool = True
    bmkg_interval_seconds: int = 60
    max_photo_mb: int = 10
    max_import_mb: int = 150
    site_title: str = "GRIDCAST"
    site_subtitle: str = "Weather Intelligence & Grid Asset Monitoring"
    site_unit: str = "PLN UP2D Sumatera Barat"

    def ip_allowed(self, ip: str | None) -> bool:
        if not self.allowed_networks:
            return True
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return any(addr in net for net in self.allowed_networks)


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or BASE_DIR / ".env")
    e = os.environ.get
    nets = []
    for part in (e("DCC_ALLOWED_NETWORKS", "10.17.119.0/24,127.0.0.1/32,::1/128")).split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    data_dir = Path(e("DCC_DATA_DIR", "") or (BASE_DIR / "data"))
    secret = e("DCC_SECRET_KEY", "")
    if not secret:
        # Persist a generated secret so sessions survive restarts.
        data_dir.mkdir(parents=True, exist_ok=True)
        f = data_dir / ".secret_key"
        if f.exists():
            secret = f.read_text().strip()
        else:
            secret = secrets.token_hex(32)
            f.write_text(secret)
    return Settings(
        host=e("DCC_HOST", "0.0.0.0"),
        port=int(e("DCC_PORT", "18085")),
        database_url=e("DCC_DATABASE_URL", Settings.database_url),
        secret_key=secret,
        allowed_networks=nets,
        trust_proxy_headers=_bool(e("DCC_TRUST_PROXY_HEADERS"), False),
        data_dir=data_dir,
        session_hours=int(e("DCC_SESSION_HOURS", "12")),
        cookie_secure=_bool(e("DCC_COOKIE_SECURE"), False),
        initial_admin_user=e("DCC_INITIAL_ADMIN_USER", "admin"),
        initial_admin_password=e("DCC_INITIAL_ADMIN_PASSWORD", ""),
        collector_enabled=_bool(e("DCC_COLLECTOR_ENABLED"), True),
        bmkg_interval_seconds=max(30, int(e("DCC_BMKG_INTERVAL", "60"))),
        max_photo_mb=int(e("DCC_MAX_PHOTO_MB", "10")),
        max_import_mb=int(e("DCC_MAX_IMPORT_MB", "150")),
        site_title=e("DCC_SITE_TITLE", Settings.site_title),
        site_subtitle=e("DCC_SITE_SUBTITLE", Settings.site_subtitle),
        site_unit=e("DCC_SITE_UNIT", Settings.site_unit),
    )
