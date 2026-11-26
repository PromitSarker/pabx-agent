"""Admin authentication: one password (ADMIN_PASSWORD in .env) -> signed, expiring bearer token. Stateless, so it works across workers."""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Dict, List

from fastapi import Header, HTTPException, Request

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
_SECRET = (os.getenv("ADMIN_TOKEN_SECRET") or hashlib.sha256(("admin-token:" + ADMIN_PASSWORD).encode()).hexdigest()).encode()
TOKEN_TTL_SECONDS = 12 * 3600
_fails: Dict[str, List[float]] = {}  # per worker; enough to slow down guessing


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token() -> str:
    payload = _b64(json.dumps({"exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode())
    sig = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str) -> bool:
    try:
        payload, sig = token.split(".", 1)
        good = _b64(hmac.new(_SECRET, payload.encode(), hashlib.sha256).digest())
        return hmac.compare_digest(sig, good) and json.loads(_unb64(payload)).get("exp", 0) > time.time()
    except Exception:
        return False


def require_admin(authorization: str = Header("")) -> None:
    """FastAPI dependency for every admin route."""
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not configured on the server; the admin API is disabled")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not verify_token(token.strip()):
        raise HTTPException(401, "admin login required")


def login(password: str, request: Request) -> str:
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not configured on the server")
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 8:
        raise HTTPException(429, "too many wrong passwords, try again in a few minutes")
    if not hmac.compare_digest((password or "").encode(), ADMIN_PASSWORD.encode()):
        recent.append(now)
        _fails[ip] = recent
        raise HTTPException(401, "wrong password")
    _fails.pop(ip, None)
    return make_token()
