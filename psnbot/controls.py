"""Telegram menu for turning store notifications on/off.

/menu (or /start) shows one button per store plus "All on" / "All off".
Muted stores are still checked, so turning one back on doesn't send stale alerts.
"""
from __future__ import annotations

import asyncio
import html
import logging

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "menu", "description": "Turn store notifications on/off"},
    {"command": "status", "description": "What's in stock right now"},
    {"command": "help", "description": "How this bot works"},
]


class Controls:
    def __init__(self, bot):
        self.bot = bot          # psnbot.main.Bot
        self.tg = bot.tg

    # -- settings --------------------------------------------------------
    @property
    def muted(self) -> set[str]:
        return set(self.bot.state.settings.setdefault("muted", []))

    def set_muted(self, muted: set[str]) -> None:
        self.bot.state.settings["muted"] = sorted(muted)
        self.bot.state.save()

    # -- views -----------------------------------------------------------
    def _in_stock(self, store_id: str) -> int:
        return sum(1 for r in self.bot.state.items.values() if r.get("store") == store_id and r.get("in_stock"))

    def menu_text(self) -> str:
        muted = self.muted
        lines = ["🛎 <b>Notifications</b>", ""]
        for s in self.bot.stores:
            on = s.id not in muted
            n = self._in_stock(s.id)
            stock = f" · {n} in stock" if n else ""
            lines.append(f"{'✅' if on else '🔕'} <b>{html.escape(s.name)}</b>: {'on' if on else 'off'}{stock}")
        lines += ["", "Tap a store to switch it on/off.",
                  "<i>Muted stores keep being checked, so switching one back on only sends new restocks.</i>"]
        return "\n".join(lines)

    def menu_markup(self) -> dict:
        muted = self.muted
        buttons = [{"text": f"{'✅' if s.id not in muted else '🔕'} {s.name}", "callback_data": f"t:{s.id}"}
                   for s in self.bot.stores]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{"text": "🔔 All on", "callback_data": "all:on"},
                     {"text": "🔕 All off", "callback_data": "all:off"}])
        rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
        return {"inline_keyboard": rows}

    def status_text(self) -> str:
        muted = self.muted
        lines = ["📦 <b>In stock right now</b>", ""]
        any_stock = False
        for s in self.bot.stores:
            recs = [r for r in self.bot.state.items.values() if r.get("store") == s.id and r.get("in_stock")]
            err = (self.bot.state.store_errors.get(s.id) or {}).get("count", 0)
            tag = " 🔕" if s.id in muted else ""
            if recs:
                any_stock = True
                lines.append(f"<b>{html.escape(s.name)}</b>{tag}")
                for r in sorted(recs, key=lambda r: r.get("price") or 0):
                    price = f" — ₹{r['price']:,.0f}" if r.get("price") else ""
                    lines.append(f'• <a href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a>{price}')
            elif err >= self.bot.error_alert_after:
                lines.append(f"<b>{html.escape(s.name)}</b>{tag}: ⚠️ can't be checked right now")
        if not any_stock:
            lines.append("Nothing is in stock at the moment.")
        return "\n".join(lines)

    HELP = ("🤖 I check PlayStation gift card stock and message you when a card comes back.\n\n"
            "/menu: switch notifications per store, or all at once\n"
            "/status: what's in stock right now\n\n"
            "Turning a store off only stops its messages; it's still checked in the background.")

    # -- handling --------------------------------------------------------
    def _allowed(self, chat_id) -> bool:
        return str(chat_id) in {str(c) for c in self.tg.chat_ids}

    async def handle(self, client, upd: dict) -> None:
        if "callback_query" in upd:
            await self._on_button(client, upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        if not self._allowed(chat_id):
            log.warning("Ignoring command from unknown chat %s", chat_id)
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/menu", "/start", "/settings", "/notifications"):
            await self.tg.send(client, self.menu_text(), chat_ids=[chat_id], reply_markup=self.menu_markup())
        elif cmd == "/status":
            await self.tg.send(client, self.status_text(), chat_ids=[chat_id])
        elif cmd == "/help":
            await self.tg.send(client, self.HELP, chat_ids=[chat_id])

    async def _on_button(self, client, cq: dict) -> None:
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not self._allowed(chat_id):
            await self.tg.answer_callback(client, cq["id"], "Not allowed")
            return
        data = cq.get("data") or ""
        ids = {s.id: s.name for s in self.bot.stores}
        muted = self.muted
        if data.startswith("t:") and data[2:] in ids:
            sid = data[2:]
            muted ^= {sid}
            note = f"{ids[sid]}: {'off 🔕' if sid in muted else 'on ✅'}"
        elif data == "all:on":
            muted, note = set(), "All stores on ✅"
        elif data == "all:off":
            muted, note = set(ids), "All stores off 🔕"
        else:
            note = "Updated"
        if data != "refresh":
            self.set_muted(muted)
            log.info("Notifications: %s (muted: %s)", note, sorted(muted) or "none")
        await self.tg.answer_callback(client, cq["id"], note)
        await self.tg.edit(client, chat_id, msg.get("message_id"), self.menu_text(), self.menu_markup())

    # -- polling loop ----------------------------------------------------
    async def run(self, client, stop: asyncio.Event) -> None:
        await self.tg.api(client, "setMyCommands", {"commands": COMMANDS})
        offset = self.bot.state.settings.get("tg_offset")
        while not stop.is_set():
            try:
                payload = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
                if offset:
                    payload["offset"] = offset
                updates = await self.tg.api(client, "getUpdates", payload, timeout=65)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram polling error: %r", e)
                updates = None
            if updates is None:
                # Error (e.g. 409 "another copy of this bot is polling") -> back off, don't spin.
                await asyncio.sleep(5)
                continue
            if not updates:
                await asyncio.sleep(0.5)   # normal long-poll timeout with nothing new
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    await self.handle(client, upd)
                except Exception:  # noqa: BLE001
                    log.exception("Failed to handle Telegram update")
            if updates:
                self.bot.state.settings["tg_offset"] = offset
                self.bot.state.save()
