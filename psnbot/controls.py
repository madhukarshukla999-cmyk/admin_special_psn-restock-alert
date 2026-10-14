"""Telegram commands and buttons.

/menu      per-person store on/off, All on/off, sold-out alerts, daily summary
/status    what's in stock now
/history   restocks in the last 7 days (how long each lasted)
/besttime  when each store usually restocks
/check     check every store right now
/health    is each store being checked OK
/users     (admin) people who get alerts, approve/remove
/update    (admin) install the latest version now
New people send /start -> admins get Allow / Deny buttons.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from . import history as hist

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "menu", "description": "Turn notifications on/off"},
    {"command": "status", "description": "What's in stock right now"},
    {"command": "check", "description": "Check every store now"},
    {"command": "history", "description": "Restocks in the last 7 days"},
    {"command": "besttime", "description": "When stores usually restock"},
    {"command": "health", "description": "Is every store being checked OK"},
    {"command": "users", "description": "(owner) Who gets alerts"},
    {"command": "update", "description": "(owner) Install the latest version"},
    {"command": "help", "description": "How this bot works"},
]


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    return f"{hist.fmt_duration(time.time() - ts)} ago"


class Controls:
    def __init__(self, bot):
        self.bot = bot          # psnbot.main.Bot
        self.tg = bot.tg
        self.users = bot.users
        self._last_manual_check = 0.0

    def _save(self) -> None:
        self.bot.state.save()

    # -- views -----------------------------------------------------------
    def _in_stock(self, store_id: str) -> int:
        return sum(1 for r in self.bot.state.items.values() if r.get("store") == store_id and r.get("in_stock"))

    def menu_text(self, cid) -> str:
        muted = self.users.muted(cid)
        lines = ["🛎 <b>Your notifications</b>", ""]
        for s in self.bot.stores:
            on = s.id not in muted
            n = self._in_stock(s.id)
            lines.append(f"{'✅' if on else '🔕'} <b>{html.escape(s.name)}</b>: {'on' if on else 'off'}"
                         + (f" · {n} in stock" if n else ""))
        lines += ["",
                  f"🔴 Sold-out alerts: <b>{'on' if self.users.pref(cid, 'soldout') else 'off'}</b>",
                  f"📰 Daily summary: <b>{'on' if self.users.pref(cid, 'summary') else 'off'}</b>",
                  "", "Tap a button to switch it.",
                  "<i>Muted stores keep being checked, so switching one back on only sends new restocks.</i>"]
        return "\n".join(lines)

    def menu_markup(self, cid) -> dict:
        muted = self.users.muted(cid)
        buttons = [{"text": f"{'✅' if s.id not in muted else '🔕'} {s.name}", "callback_data": f"t:{s.id}"}
                   for s in self.bot.stores]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{"text": "🔔 All on", "callback_data": "all:on"},
                     {"text": "🔕 All off", "callback_data": "all:off"}])
        so, su = self.users.pref(cid, "soldout"), self.users.pref(cid, "summary")
        rows.append([{"text": f"{'✅' if so else '🔕'} Sold-out alerts", "callback_data": "p:soldout"},
                     {"text": f"{'✅' if su else '🔕'} Daily summary", "callback_data": "p:summary"}])
        rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
        return {"inline_keyboard": rows}

    def status_text(self, cid) -> str:
        muted = self.users.muted(cid)
        lines = ["📦 <b>In stock right now</b>", ""]
        any_stock = False
        for s in self.bot.stores:
            recs = [(k, r) for k, r in self.bot.state.items.items() if r.get("store") == s.id and r.get("in_stock")]
            err = (self.bot.state.store_errors.get(s.id) or {}).get("count", 0)
            tag = " 🔕" if s.id in muted else ""
            if recs:
                any_stock = True
                lines.append(f"<b>{html.escape(s.name)}</b>{tag}")
                for key, r in sorted(recs, key=lambda kr: kr[1].get("price") or 0):
                    price = f" — ₹{r['price']:,.0f}" if r.get("price") else ""
                    since = hist.open_since(self.bot.state.history, key)
                    lasted = f" (for {hist.fmt_duration(time.time() - since)})" if since else ""
                    lines.append(f'• <a href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a>'
                                 f"{price}{lasted}")
            elif err >= self.bot.error_alert_after:
                lines.append(f"<b>{html.escape(s.name)}</b>{tag}: ⚠️ can't be checked right now")
        if not any_stock:
            lines.append("Nothing is in stock at the moment.")
        return "\n".join(lines)

    def history_text(self, days: int = 7) -> str:
        names = {s.id: s.name for s in self.bot.stores}
        evs = hist.recent(self.bot.state.history, days * 86400)
        if not evs:
            return f"📜 No restocks recorded in the last {days} days."
        lines = [f"📜 <b>Restocks in the last {days} days</b> ({len(evs)})", ""]
        for e in sorted(evs, key=lambda e: e["start"], reverse=True)[:40]:
            when = hist._ist(e["start"]).strftime("%d %b %I:%M %p")
            lasted = "still in stock" if e.get("end") is None else f"lasted {hist.fmt_duration(e['end'] - e['start'])}"
            price = f" ₹{e['price']:,.0f}" if e.get("price") else ""
            lines.append(f"• {when} · <b>{html.escape(names.get(e['store'], e['store']))}</b> · "
                         f"{html.escape(e['title'][:55])}{price} · {lasted}")
        if len(evs) > 40:
            lines.append(f"…and {len(evs) - 40} older")
        return "\n".join(lines)

    def besttime_text(self) -> str:
        names = {s.id: s.name for s in self.bot.stores}
        lines = hist.best_times(self.bot.state.history, names)
        if not lines:
            return ("⏰ No restocks recorded yet. Once cards come back a few times on different days, "
                    "I'll tell you when each store usually restocks.")
        return "⏰ <b>When stores usually restock</b> (last 30 days)\n\n" + "\n".join(lines) + \
               "\n\n<i>Needs at least 3 restocks on 2+ days per store.</i>"

    def health_text(self) -> str:
        b = self.bot
        lines = ["🩺 <b>Health</b>", ""]
        for s in b.stores:
            e = b.state.store_errors.get(s.id) or {}
            if e.get("count"):
                lines.append(f"⚠️ <b>{html.escape(s.name)}</b>: failing ({e['count']} in a row), last OK "
                             f"{_ago(e.get('last_ok'))}\n    <code>{html.escape((e.get('last_error') or '')[:150])}</code>")
            else:
                took = f", took {e['took']:.1f}s" if e.get("took") else ""
                lines.append(f"✅ <b>{html.escape(s.name)}</b>: OK, checked {_ago(e.get('last_ok'))}{took}")
        st = b.state.settings
        lines += ["",
                  f"🤖 Running for {hist.fmt_duration(time.time() - b.started)} · version {html.escape(b.version)}",
                  f"🔁 Full check every ~{b.interval}s, fast check every {b.fast_interval}s",
                  f"👥 {len(self.users.active())} people get alerts"
                  + (f", {len(self.users.pending())} waiting for approval" if self.users.pending() else ""),
                  f"⬆️ Auto-update: {'on' if b.auto_update else 'off'}"
                  + (f", last checked {_ago(st.get('last_update_check'))}" if b.auto_update else "")]
        return "\n".join(lines)

    def users_view(self) -> tuple[str, dict]:
        lines, rows = ["👥 <b>People who get alerts</b>", ""], []
        for cid, rec in self.users.all.items():
            name = html.escape(rec.get("name") or cid)
            if rec.get("admin"):
                lines.append(f"👑 {name} (<code>{cid}</code>) · owner")
            elif rec.get("approved"):
                lines.append(f"✅ {name} (<code>{cid}</code>)")
                rows.append([{"text": f"❌ Remove {rec.get('name') or cid}"[:40], "callback_data": f"u:rm:{cid}"}])
            elif rec.get("pending"):
                lines.append(f"⏳ {name} (<code>{cid}</code>) · waiting")
                rows.append([{"text": f"✅ Allow {rec.get('name') or cid}"[:40], "callback_data": f"u:ok:{cid}"},
                             {"text": "❌ Deny", "callback_data": f"u:no:{cid}"}])
        lines += ["", "To add someone: they open this bot in Telegram and tap <b>Start</b>. "
                      "You'll get Allow / Deny buttons."]
        return "\n".join(lines), {"inline_keyboard": rows or [[{"text": "🔄 Refresh", "callback_data": "u:list"}]]}

    HELP = ("🤖 I check PlayStation gift card stock and message you when a card comes back "
            "(and when it sells out).\n\n"
            "/menu: switch notifications per store or all at once, sold-out alerts, daily summary\n"
            "/status: what's in stock right now\n"
            "/check: check every store now\n"
            "/history: restocks in the last 7 days\n"
            "/besttime: when each store usually restocks\n"
            "/health: is every store being checked OK\n\n"
            "Owner only: /users (who gets alerts), /update (install the latest version)\n\n"
            "Turning a store off only stops its messages; it's still checked in the background.")

    # -- handling --------------------------------------------------------
    async def handle(self, client, upd: dict) -> None:
        if "callback_query" in upd:
            await self._on_button(client, upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not cid or not text.startswith("/"):
            return
        cid = str(cid)
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        send = lambda t, **kw: self.tg.send(client, t, chat_ids=[cid], **kw)  # noqa: E731

        if not self.users.is_allowed(cid):
            if cmd != "/start":
                await send("🔒 You don't have access yet. Send /start to ask the owner.")
                return
            name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("title") or cid
            if chat.get("username"):
                name += f" (@{chat['username']})"
            if self.users.request(cid, name):
                self._save()
                log.info("Access request from %s %s", cid, name)
                await self.tg.send(client, f"🙋 <b>{html.escape(name)}</b> wants PlayStation card alerts.",
                                   chat_ids=self.users.recipients(admins_only=True),
                                   reply_markup={"inline_keyboard": [[
                                       {"text": "✅ Allow", "callback_data": f"u:ok:{cid}"},
                                       {"text": "❌ Deny", "callback_data": f"u:no:{cid}"}]]})
            await send("👋 Request sent to the bot's owner. You'll get a message once you're approved.")
            return

        admin = self.users.is_admin(cid)
        if cmd in ("/menu", "/start", "/settings", "/notifications"):
            await send(self.menu_text(cid), reply_markup=self.menu_markup(cid))
        elif cmd == "/status":
            await send(self.status_text(cid))
        elif cmd == "/history":
            days = int(args[0]) if args and args[0].isdigit() else 7
            await send(self.history_text(max(1, min(days, 90))))
        elif cmd in ("/besttime", "/best"):
            await send(self.besttime_text())
        elif cmd == "/health":
            await send(self.health_text())
        elif cmd == "/check":
            wait = 60 - (time.time() - self._last_manual_check)
            if wait > 0:
                await send(f"⏳ A check just ran. Try again in {int(wait)} s.")
            else:
                self._last_manual_check = time.time()
                await send("🔎 Checking every store now… I'll reply when it's done.")
                self.bot.request_check(cid)
        elif cmd == "/users" and admin:
            text, kb = self.users_view()
            await send(text, reply_markup=kb)
        elif cmd == "/update" and admin:
            await send("⬆️ Checking for updates…")
            await self.bot.run_update(client, manual_cid=cid)
        elif cmd == "/help":
            await send(self.HELP)
        elif cmd in ("/users", "/update"):
            await send("Only the owner can use that.")

    async def _on_button(self, client, cq: dict) -> None:
        msg = cq.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id"))
        data = cq.get("data") or ""
        if not self.users.is_allowed(cid):
            await self.tg.answer_callback(client, cq["id"], "Not allowed")
            return

        if data.startswith("u:"):
            await self._on_user_button(client, cq, cid, data)
            return

        ids = {s.id: s.name for s in self.bot.stores}
        muted = self.users.muted(cid)
        note = "Updated"
        if data.startswith("t:") and data[2:] in ids:
            sid = data[2:]
            muted ^= {sid}
            note = f"{ids[sid]}: {'off 🔕' if sid in muted else 'on ✅'}"
        elif data == "all:on":
            muted, note = set(), "All stores on ✅"
        elif data == "all:off":
            muted, note = set(ids), "All stores off 🔕"
        elif data in ("p:soldout", "p:summary"):
            on = self.users.toggle_pref(cid, data[2:])
            note = f"{'Sold-out alerts' if data == 'p:soldout' else 'Daily summary'}: {'on ✅' if on else 'off 🔕'}"
        if data != "refresh":
            self.users.set_muted(cid, muted)
            self._save()
            log.info("%s settings: %s", cid, note)
        await self.tg.answer_callback(client, cq["id"], note)
        await self.tg.edit(client, cid, msg.get("message_id"), self.menu_text(cid), self.menu_markup(cid))

    async def _on_user_button(self, client, cq, cid, data) -> None:
        if not self.users.is_admin(cid):
            await self.tg.answer_callback(client, cq["id"], "Only the owner can do that")
            return
        _, action, *rest = data.split(":")
        target = rest[0] if rest else ""
        rec = self.users.get(target) or {}
        name = rec.get("name") or target
        note = ""
        if action == "ok" and rec:
            self.users.approve(target)
            note = f"{name} can now get alerts"
            await self.tg.send(client, "✅ You're in! You'll get PlayStation card alerts here.\n"
                                       "Send /menu to choose stores, /help for everything else.",
                               chat_ids=[target])
        elif action in ("no", "rm") and rec:
            self.users.remove(target)
            note = f"{name} removed"
            if action == "rm":
                await self.tg.send(client, "You've been removed from PlayStation card alerts.", chat_ids=[target])
        elif action != "list":
            note = "Already handled"
        self._save()
        if note:
            log.info("Users: %s", note)
        await self.tg.answer_callback(client, cq["id"], note or "Updated")
        text, kb = self.users_view()
        await self.tg.edit(client, cid, (cq.get("message") or {}).get("message_id"), text, kb)

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
