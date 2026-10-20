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


def setup_code(state, chat_id, token: str) -> str:
    """One code for the extension's setup screen: feed + owner chat + bot token (base64 JSON)."""
    import base64
    raw = json.dumps({"v": 1, "s": server(), "t": topic(state), "c": str(chat_id), "k": token}, separators=(",", ":"))
    return "psnbot1:" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


async def publish_control(client, state) -> bool:
    """Tell every connected extension whether it may act (paused = Telegram alerts only)."""
    payload = {"v": 1, "type": "control", "paused": paused(state), "ts": time.time()}
    try:
        r = await client.post(f"{server()}/{topic(state)}", content=json.dumps(payload).encode(),
                              headers={"Tags": "psnctl", "Title": "extension control"}, timeout=10)
        return r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.warning("Extension control push failed: %r", e)
        return False
