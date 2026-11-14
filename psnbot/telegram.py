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
        # Messages to different people go out at the same time (an alert to 20 people used to take
        # 20 round trips). 8 at once stays under Telegram's ~30 messages/second bot limit; a 429
        # still waits for `retry_after` below.
        self._sem = asyncio.Semaphore(8)

    # -- low level -------------------------------------------------------
    async def api(self, client: httpx.AsyncClient, method: str, payload: dict,
                  timeout: float = 20) -> dict | None:
        """Call a Bot API method. Returns `result` or None on failure."""
        if self.dry_run:
            log.info("[dry-run] Telegram %s %s", method, {k: v for k, v in payload.items() if k != "text"})
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(4):
            try:
                r = await client.post(url, json=payload, timeout=timeout)
                data = r.json()
                if r.status_code == 429:
                    await asyncio.sleep(float((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                    continue
                if not data.get("ok"):
                    # "message is not modified" is harmless (user tapped the same state twice)
                    if "not modified" not in str(data.get("description", "")):
                        log.error("Telegram %s error %s: %s", method, r.status_code, str(data)[:300])
                    return None
                return data.get("result")
            except (httpx.HTTPError, ValueError) as e:
                if method == "getUpdates":
                    raise
                log.warning("Telegram %s failed (attempt %d): %r", method, attempt + 1, e)
                await asyncio.sleep(3 * (attempt + 1))
        return None

    # -- messages --------------------------------------------------------
    async def send(self, client: httpx.AsyncClient, html: str, chat_ids: list[str] | None = None,
                   reply_markup: dict | None = None) -> bool:
        chunks = _split(html)
        targets = list(dict.fromkeys(self.chat_ids if chat_ids is None else chat_ids))

        async def to(chat_id) -> bool:          # one person: their chunks in order
            ok = True
            for chunk in chunks:
                if self.dry_run:
                    log.info("[dry-run] Telegram -> %s:\n%s", chat_id, chunk)
                    continue
                payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                           "disable_web_page_preview": True}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                async with self._sem:
                    ok &= await self.api(client, "sendMessage", payload) is not None
            return ok

        if len(targets) == 1:
            return await to(targets[0])
        return all(await asyncio.gather(*(to(c) for c in targets)))

    async def edit(self, client, chat_id, message_id, html: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": html,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.api(client, "editMessageText", payload)

    async def answer_callback(self, client, callback_id: str, text: str = "") -> None:
        await self.api(client, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


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
