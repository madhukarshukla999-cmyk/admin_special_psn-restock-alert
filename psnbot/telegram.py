from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 4000  # Telegram hard limit is 4096


class Telegram:
    def __init__(self, token: str, chat_ids: list[str], dry_run: bool = False):
        self.token = token
        self.chat_ids = chat_ids
        self.dry_run = dry_run

    async def send(self, client: httpx.AsyncClient, html: str) -> bool:
        ok = True
        for chunk in _split(html):
            for chat_id in self.chat_ids:
                ok &= await self._send_one(client, chat_id, chunk)
        return ok

    async def _send_one(self, client, chat_id: str, text: str) -> bool:
        if self.dry_run:
            log.info("[dry-run] Telegram -> %s:\n%s", chat_id, text)
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(4):
            try:
                r = await client.post(url, json=payload, timeout=20)
                if r.status_code == 429:
                    wait = (r.json().get("parameters") or {}).get("retry_after", 5)
                    await asyncio.sleep(float(wait) + 1)
                    continue
                if r.status_code != 200:
                    log.error("Telegram error %s: %s", r.status_code, r.text[:300])
                    return False
                return True
            except httpx.HTTPError as e:
                log.warning("Telegram send failed (attempt %d): %r", attempt + 1, e)
                await asyncio.sleep(3 * (attempt + 1))
        return False


def _split(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > MAX_LEN:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{block}" if cur else block[:MAX_LEN]
    if cur:
        parts.append(cur)
    return parts
