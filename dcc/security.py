"""Password hashing, sessions, roles and network allow-list."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets

ROLE_RANK = {"guest": 0, "user": 1, "admin": 2}
SESSION_COOKIE = "dcc_session"

# scrypt parameters (~16 MB, ~50 ms) — stdlib, tanpa dependensi native.
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Kata sandi minimal 8 karakter.")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def token_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def new_session(db, user_id: int, secret: str, hours: int, ip: str | None, ua: str | None) -> str:
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    with db.tx() as t:
        t.run("DELETE FROM sessions WHERE expires_at < now()")
        t.run("INSERT INTO sessions(token_hash,user_id,expires_at,ip,user_agent) VALUES (%s,%s,%s,%s,%s)",
              (token_hash(token, secret), user_id, expires, ip, (ua or "")[:300]))
        t.run("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))
    return token


def session_user(db, token: str | None, secret: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    return db.one(
        "SELECT u.id, u.username, u.display_name, u.role, u.must_change_password FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s AND s.expires_at > now() AND u.active",
        (token_hash(token, secret),))


def drop_session(db, token: str | None, secret: str) -> None:
    if token:
        db.run("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token, secret),))


def ensure_initial_admin(db, username: str, password: str) -> str | None:
    """Create the first admin if no admin exists. Returns generated password if any."""
    row = db.one("SELECT count(*) AS n FROM users WHERE role = 'admin'")
    if row and row["n"]:
        return None
    generated = None
    if not password:
        generated = password = secrets.token_urlsafe(9)
    db.run("INSERT INTO users(username, display_name, role, password_hash, must_change_password) "
           "VALUES (%s, %s, 'admin', %s, true) ON CONFLICT (username) DO NOTHING",
           (username, "Administrator", hash_password(password)))
    return generated


def has_role(user: dict | None, needed: str) -> bool:
    role = user["role"] if user else "guest"
    return ROLE_RANK.get(role, 0) >= ROLE_RANK[needed]
