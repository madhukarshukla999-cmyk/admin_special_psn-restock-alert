"""Add a GamesTheShop account from the browser (any login method: Google / Gmail, OTP, …).

Google sign-in only works in a real browser, so the server can't do it. Instead:
  1. /buy → 🌐 Add account from browser: the bot makes a one-time code (shown only to the owner).
  2. The owner logs in on gamestheshop.com in Chrome, pastes the code in the extension's Options
     and presses Send. The extension reads the site's login (localStorage) and sends it, encrypted
     with the code, to a ntfy topic that is also derived from the code.
  3. The bot (polling that topic for 10 minutes) decrypts it, checks it with GamesTheShop and
     saves it like an OTP login.

Encryption without extra packages (the same steps are in extension/options.js, WebCrypto):
  key    = UTF-8 of the code (upper-case, no dashes)
  topic  = "psngl-" + sha256("topic:" + key)[:24]
  stream = sha256(key || nonce || counter_be32) blocks, XORed with the JSON
  packet = base64url(nonce16 || ciphertext || hmac_sha256(key, nonce || ciphertext))
A code works once and for 10 minutes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

log = logging.getLogger(__name__)

TTL = 10 * 60
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"        # no 0/O/1/I/L: easy to type


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def _key(code: str) -> bytes:
    return code.replace("-", "").replace(" ", "").upper().encode()


def topic_for(code: str) -> str:
    return "psngl-" + hashlib.sha256(b"topic:" + _key(code)).hexdigest()[:24]


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def decrypt(code: str, packet: str) -> dict:
    key = _key(code)
    raw = base64.urlsafe_b64decode(packet + "=" * (-len(packet) % 4))
    if len(raw) < 16 + 32:
        raise ValueError("too short")
    nonce, ct, mac = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(mac, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("wrong code or damaged message")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(key, nonce, len(ct)))).decode())


def encrypt(code: str, data: dict) -> str:       # used by the tests (the extension does this part)
    key, nonce = _key(code), secrets.token_bytes(16)
    pt = json.dumps(data).encode()
    ct = bytes(a ^ b for a, b in zip(pt, _stream(key, nonce, len(pt))))
    return base64.urlsafe_b64encode(nonce + ct + hmac.new(key, nonce + ct, hashlib.sha256).digest()).decode().rstrip("=")


def tokens_from(storage: dict) -> tuple[str, str, dict]:
    """(access, refresh, user) from the site's localStorage, whatever the exact key names."""
    def get(*names):
        for n in names:
            for k, v in storage.items():
                if k.lower() == n.lower() and v:
                    return v
        return ""
    access = get("accessToken", "access_token", "access", "token", "authToken")
    refresh = get("refreshToken", "refresh_token", "refresh")
    if not access:   # some sites keep both in one JSON blob
        for v in storage.values():
            try:
                d = json.loads(v) if isinstance(v, str) else v
            except ValueError:
                continue
            if isinstance(d, dict) and (d.get("access") or d.get("accessToken")):
                access = d.get("access") or d.get("accessToken")
                refresh = refresh or d.get("refresh") or d.get("refreshToken") or ""
                break
    user = {}
    try:
        user = json.loads(get("user", "userInfo", "profile") or "{}")
    except ValueError:
        pass
    return str(access or ""), str(refresh or ""), user if isinstance(user, dict) else {}


async def wait_for_login(client, code: str, stop_at: float) -> dict | None:
    """Poll the code's ntfy topic until the browser's message arrives (or time runs out)."""
    from . import extension_push as ep
    url = f"{ep.server()}/{topic_for(code)}/json"
    while time.time() < stop_at:
        try:
            r = await client.get(url, params={"poll": "1", "since": "15m"}, timeout=15)
            for line in r.text.strip().splitlines():
                try:
                    msg = json.loads(line).get("message") or ""
                    return decrypt(code, msg)
                except (ValueError, KeyError, UnicodeDecodeError):
                    continue
        except Exception:  # noqa: BLE001 - keep polling
            log.debug("browser-login poll failed", exc_info=True)
        await asyncio.sleep(3)
    return None


async def add_from_browser(bot, client, data: dict) -> tuple[str, dict]:
    """Check the browser's login with GamesTheShop and save it. Returns (label, account)."""
    from . import gtscheckout as gc
    access, refresh, user = tokens_from(data.get("storage") or {})
    if not access:
        raise gc.GtsError("the browser isn't logged in on gamestheshop.com (no login found)")
    acc = {"access": access, "refresh": refresh}
    r = await gc._get(None, "/customer/profile", headers=gc._auth(acc))
    if r.status_code in (401, 403):
        raise gc.GtsError("GamesTheShop didn't accept that login (log in again on the site, then resend)")
    if r.status_code != 200:
        raise gc.GtsError(f"couldn't check the login (HTTP {r.status_code})")
    prof = gc._json(r).get("data") or {}
    mobile = str(prof.get("mobile_number") or prof.get("mobile") or prof.get("phone") or user.get("mobile_number") or "")
    mobile = "".join(ch for ch in mobile if ch.isdigit())[-10:]
    email = str(prof.get("email") or user.get("email") or "")
    label = mobile or email or (str(prof.get("name") or user.get("name") or "") or f"web-{int(time.time()) % 100000}")
    acc.update(mobile=mobile, email=email, user_id=str(prof.get("user_id") or prof.get("id") or ""),
               addresses=prof.get("addresses") if isinstance(prof.get("addresses"), list) else None,
               pending_otp=False, added=time.time(), via="browser")
    store = gc.accounts(bot.state)
    store.setdefault(label, {}).update(acc)
    store[label].pop("expired", None)
    store[label].pop("expired_told", None)
    await bot.state.save_async()
    return label, store[label]
