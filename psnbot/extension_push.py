"""Push restocks to the browser extension (extension/ folder) through ntfy.sh.

The bot POSTs a small JSON message to a private ntfy topic; the extension on the laptop
listens to that topic and opens the card's page (and clicks BUY NOW on supported stores).
The topic name is a long random secret, created once and saved in the bot's state.
Override with NTFY_TOPIC / NTFY_SERVER env vars. Nothing is ever bought automatically.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time

log = logging.getLogger(__name__)


def server() -> str:
    return (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")


def topic(state) -> str:
    t = os.environ.get("NTFY_TOPIC", "").strip()
    if t:
        return t
    t = state.settings.get("ext_topic")
    if not t:
        t = "psnbot-" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "y")
        state.settings["ext_topic"] = t
        state.save()
    return t


def cart_url(it) -> str | None:
    if it.store == "amazon" and len(it.item_id) == 10:
        return f"https://www.amazon.in/gp/aws/cart/add.html?ASIN.1={it.item_id}&Quantity.1=1"
    return None


async def publish(client, state, store_name: str, items: list, test: bool = False) -> bool:
    payload = {
        "v": 1, "ts": time.time(), "test": test, "store": items[0].store if items else "",
        "store_name": store_name,
        "items": [{"id": it.item_id, "title": it.title, "url": it.url, "cart_url": cart_url(it),
                   "value": it.denomination, "price": it.price} for it in items],
    }
    try:
        r = await client.post(f"{server()}/{topic(state)}", content=json.dumps(payload).encode(),
                              headers={"Title": f"PSN card back on {store_name}", "Tags": "video_game"},
                              timeout=10)
        if r.status_code >= 300:
            log.warning("Extension push failed: HTTP %s %s", r.status_code, r.text[:150])
            return False
        return True
    except Exception as e:  # noqa: BLE001 - never block alerts
        log.warning("Extension push failed: %r", e)
        return False


def paused(state) -> bool:
    return bool(state.settings.get("ext_paused"))


QR_MODES = {"dm": "📷 Send QR in DM", "notify": "🔔 Pay manually (notification only)", "off": "⛔ Off (nothing to Telegram)"}


def qr_mode(state) -> str:
    """'' = never set from Telegram (each browser keeps its own Options choice)."""
    m = state.settings.get("ext_qr_mode", "")
    return m if m in QR_MODES else ""


def setup_code(state, chat_id, token: str) -> str:
    """One code for the extension's setup screen: feed + owner chat + bot token (base64 JSON)."""
    import base64
    raw = json.dumps({"v": 1, "s": server(), "t": topic(state), "c": str(chat_id), "k": token}, separators=(",", ":"))
    return "psnbot1:" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def publish_control(client, state) -> bool:
    """Tell every connected extension whether it may act (paused = Telegram alerts only)."""
    payload = {"v": 1, "type": "control", "paused": paused(state), "ts": time.time(),
               "revoked": revoked(state)}
    if qr_mode(state):
        payload["qr_mode"] = qr_mode(state)
    try:
        r = await client.post(f"{server()}/{topic(state)}", content=json.dumps(payload).encode(),
                              headers={"Tags": "psnctl", "Title": "extension control"}, timeout=10)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.warning("Extension control push failed: %r", e)
        return False


# ---- connected browsers (each extension posts a small heartbeat to <topic>-hb) ----
def hb_topic(state) -> str:
    return topic(state) + "-hb"


def revoked(state) -> list[str]:
    return list(state.settings.get("ext_revoked") or [])


async def devices(client, state) -> list[dict]:
    """Latest heartbeat per browser (ntfy keeps ~12 h), newest first; revoked ones left out."""
    try:
        r = await client.get(f"{server()}/{hb_topic(state)}/json", params={"poll": "1", "since": "24h"}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("Extension heartbeat read failed: %r", e)
        return []
    out: dict[str, dict] = {}
    gone = set(revoked(state))
    for line in r.text.splitlines():
        try:
            d = json.loads(json.loads(line).get("message") or "{}")
        except ValueError:
            continue
        if d.get("type") != "hb" or not d.get("id") or d["id"] in gone:
            continue
        if d.get("ts", 0) >= out.get(d["id"], {}).get("ts", 0):
            out[d["id"]] = d
    return sorted(out.values(), key=lambda d: -d.get("ts", 0))


async def revoke(client, state, device_id: str) -> None:
    lst = [x for x in revoked(state) if x != device_id] + [device_id]
    state.settings["ext_revoked"] = lst[-100:]
    state.save()
    await publish_control(client, state)


async def revoke_all(client, state) -> str | None:
    """Cut off every browser: tell them, then switch to a new random feed. Returns the new topic
    (None if the topic is fixed by NTFY_TOPIC)."""
    if os.environ.get("NTFY_TOPIC", "").strip():
        return None
    old = topic(state)
    try:
        await client.post(f"{server()}/{old}", timeout=10, headers={"Tags": "psnctl"},
                          content=json.dumps({"v": 1, "type": "control", "revoke_all": True, "ts": time.time(),
                                              "paused": paused(state)}).encode())
    except Exception as e:  # noqa: BLE001
        log.warning("Revoke-all push failed: %r", e)
    state.settings["ext_topic"] = None
    state.settings["ext_revoked"] = []
    state.save()
    new = topic(state)
    await publish_control(client, state)
    return new
