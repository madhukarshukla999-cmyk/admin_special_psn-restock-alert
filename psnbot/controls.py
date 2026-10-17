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
import re
import time

from . import history as hist
from .channels import CALLMEBOT_HELP, CALLMEBOT_NUMBER, EMAIL_RE, clean_phone, send_whatsapp

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "menu", "description": "Turn notifications on/off"},
    {"command": "status", "description": "What's in stock right now"},
    {"command": "check", "description": "Check every store now"},
    {"command": "history", "description": "Restocks in the last 7 days"},
    {"command": "besttime", "description": "When stores usually restock"},
    {"command": "health", "description": "Is every store being checked OK"},
    {"command": "cycles", "description": "See/change how often each store is checked"},
    {"command": "whatsapp", "description": "Get alerts on WhatsApp too"},
    {"command": "email", "description": "Get alerts by email too"},
    {"command": "users", "description": "(owner) Who gets alerts"},
    {"command": "update", "description": "(owner) Install the latest version"},
    {"command": "help", "description": "How this bot works"},
]


FULL_PRESETS = [15, 30, 60, 120, 180, 300, 600, 1800]
FAST_PRESETS = [0, 10, 15, 20, 30, 60]
RISKY = {"amazon", "flipkart", "blinkit"}      # may block very frequent checks


def fmt_secs(sec: int | None) -> str:
    if not sec:
        return "off"
    if sec < 60:
        return f"{sec} s"
    return f"{sec // 60} min" + (f" {sec % 60} s" if sec % 60 else "")


def parse_secs(raw: str) -> int | None:
    raw = raw.strip().lower()
    if raw in ("off", "0"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h)?", raw)
    if not m:
        return None
    n, unit = float(m.group(1)), (m.group(2) or "s")
    return int(n * (3600 if unit == "h" else 60 if unit.startswith("m") else 1))


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
                  self._channel_line(cid, "whatsapp"), self._channel_line(cid, "email"),
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
        ch_row = []
        for name, label in (("whatsapp", "WhatsApp"), ("email", "Email")):
            if self.users.channel(cid, name):
                on = self.users.channel_on(cid, name)
                ch_row.append({"text": f"{'✅' if on else '🔕'} {label}", "callback_data": f"p:{name}"})
        if ch_row:
            rows.append(ch_row)
        rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
        return {"inline_keyboard": rows}

    def _channel_line(self, cid, name: str) -> str:
        label = "💬 WhatsApp" if name == "whatsapp" else "📧 Email"
        ch = self.users.channel(cid, name)
        if not ch:
            return f"{label}: not set up (/{name})"
        who = ch.get("phone") or ch.get("address")
        return f"{label}: <b>{'on' if ch.get('on', True) else 'off'}</b> ({html.escape(str(who))})"

    # -- check cycles ------------------------------------------------------
    def cycles_view(self, cid) -> tuple[str, dict | None]:
        b = self.bot
        lines = ["⏱ <b>How often each store is checked</b>", ""]
        for s in b.stores:
            full, fast = b.base_intervals(s)
            dfull, dfast = b.default_intervals(s)
            extra = f" · priority cards every <b>{fmt_secs(fast)}</b>" if b.has_fast(s) else ""
            changed = " ✏️" if (full, fast) != (dfull, dfast) else ""
            lines.append(f"• <b>{html.escape(s.name)}</b>: every <b>{fmt_secs(full)}</b>{extra}{changed}")
            act = b.burst_active(s)
            if act:
                lines.append(f"    ⚡ <b>burst now</b> until {act[1] // 60:02d}:{act[1] % 60:02d} IST: every "
                             f"<b>{fmt_secs(b.burst_seconds(s))}</b> ({act[2]})")
            else:
                ws = b.burst_windows(s)
                if ws:
                    lines.append("    ⚡ burst " + ", ".join(f"{a // 60:02d}:{a % 60:02d}–{z // 60:02d}:{z % 60:02d}"
                                                          for a, z, _ in ws) + " IST")
        if not self.users.is_admin(cid):
            return "\n".join(lines), None
        lines += ["", "⚡ Burst = much faster checks around each store's usual restock time "
                  "(learned from /besttime, or set in config.yaml).",
                  "", "Tap a store to change it. ✏️ = changed from the default.",
                  "<i>Or type e.g. /cycle amazon 2m, /cycle amazon fast 30s, /cycle amazon default</i>"]
        btns = [{"text": f"⏱ {s.name}", "callback_data": f"cy:{s.id}"} for s in b.stores]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        return "\n".join(lines), {"inline_keyboard": rows}

    def cycle_store_view(self, sid: str) -> tuple[str, dict]:
        b = self.bot
        s = next(x for x in b.stores if x.id == sid)
        full, fast = b.intervals(s)
        dfull, dfast = b.default_intervals(s)
        lines = [f"⏱ <b>{html.escape(s.name)}</b>", "",
                 f"Full check: every <b>{fmt_secs(full)}</b> (default {fmt_secs(dfull)})"]
        rows = [[{"text": ("✅ " if p == full else "") + fmt_secs(p), "callback_data": f"cy:{sid}:f:{p}"}
                 for p in FULL_PRESETS[i:i + 4]] for i in range(0, len(FULL_PRESETS), 4)]
        if b.has_fast(s):
            lines.append(f"Priority cards: every <b>{fmt_secs(fast)}</b> (default {fmt_secs(dfast)})")
            rows.append([{"text": ("✅ " if (p or None) == fast else "") + "⚡" + fmt_secs(p),
                          "callback_data": f"cy:{sid}:q:{p}"} for p in FAST_PRESETS])
        if sid in RISKY and (full < 60 or (fast and fast < 30)):
            lines.append("\n⚠️ Very frequent checks can get blocked by this store for a while.")
        rows.append([{"text": "↩️ Default", "callback_data": f"cy:{sid}:r"},
                     {"text": "⬅️ All stores", "callback_data": "cy:list"}])
        return "\n".join(lines), {"inline_keyboard": rows}

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
                  "🔁 Check cycles: /cycles",
                  *([(f"🛡 WARP tunnel ({', '.join(b.names[s] for s in sorted(b.warp_stores))}): " + ("✅ up" if b.warp.alive() else f"❌ down ({html.escape(b.warp.error or '?')})")
                      + (f", restarted {b.warp.restarts}×" if b.warp.restarts else ""))] if b.warp else []),
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
            "/health: is every store being checked OK\n"
            "/cycles: how often each store is checked\n"
            "/whatsapp, /email: get alerts there too (on/off any time)\n\n"
            "Owner only: /users (who gets alerts), /update (install the latest version), changing /cycles\n\n"
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
        elif cmd in ("/cycles", "/intervals"):
            text, kb = self.cycles_view(cid)
            await send(text, **({"reply_markup": kb} if kb else {}))
        elif cmd == "/cycle":
            await send(self._cycle_command(cid, args))
        elif cmd == "/whatsapp":
            await send(await self._whatsapp_command(client, cid, args))
        elif cmd == "/email":
            await send(await self._email_command(cid, args))
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
        if data.startswith("cy:"):
            await self._on_cycle_button(client, cq, cid, data)
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
        elif data in ("p:whatsapp", "p:email") and self.users.channel(cid, data[2:]):
            ch = self.users.channel(cid, data[2:])
            ch["on"] = not ch.get("on", True)
            note = f"{'WhatsApp' if data == 'p:whatsapp' else 'Email'} alerts: {'on ✅' if ch['on'] else 'off 🔕'}"
        if data != "refresh":
            self.users.set_muted(cid, muted)
            self._save()
            log.info("%s settings: %s", cid, note)
        await self.tg.answer_callback(client, cq["id"], note)
        await self.tg.edit(client, cid, msg.get("message_id"), self.menu_text(cid), self.menu_markup(cid))

    async def _on_cycle_button(self, client, cq, cid, data) -> None:
        msg_id = (cq.get("message") or {}).get("message_id")
        if data == "cy:list":
            text, kb = self.cycles_view(cid)
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid, msg_id, text, kb)
            return
        if not self.users.is_admin(cid):
            await self.tg.answer_callback(client, cq["id"], "Only the owner can change this")
            return
        parts = data.split(":")
        sid = parts[1]
        if sid not in self.bot.names:
            await self.tg.answer_callback(client, cq["id"], "Unknown store")
            return
        note = ""
        if len(parts) == 4 and parts[2] in ("f", "q") and parts[3].isdigit():
            kind, sec = ("full" if parts[2] == "f" else "fast"), int(parts[3])
            self.bot.set_interval(sid, kind, sec)
            note = f"{self.bot.names[sid]}: {'full check' if kind == 'full' else 'priority cards'} every {fmt_secs(sec)}"
        elif len(parts) == 3 and parts[2] == "r":
            self.bot.set_interval(sid, "full", None)
            self.bot.set_interval(sid, "fast", None)
            note = f"{self.bot.names[sid]}: back to default"
        if note:
            log.info("Cycles: %s", note)
        await self.tg.answer_callback(client, cq["id"], note)
        text, kb = self.cycle_store_view(sid)
        await self.tg.edit(client, cid, msg_id, text, kb)

    def _cycle_command(self, cid, args: list[str]) -> str:
        if not self.users.is_admin(cid):
            return "Only the owner can change check cycles. See them with /cycles."
        usage = ("Usage: <code>/cycle amazon 2m</code>, <code>/cycle amazon fast 30s</code> "
                 "(or <code>fast off</code>), <code>/cycle amazon default</code>\nStores: "
                 + ", ".join(self.bot.names))
        if len(args) < 2:
            return usage
        sid = args[0].lower()
        store = next((s for s in self.bot.stores if s.id == sid or s.name.lower().startswith(sid)), None)
        if not store:
            return f"Unknown store '{html.escape(args[0])}'.\n" + usage
        if args[1].lower() in ("default", "reset"):
            self.bot.set_interval(store.id, "full", None)
            self.bot.set_interval(store.id, "fast", None)
            return f"↩️ {store.name}: back to the default ({fmt_secs(self.bot.intervals(store)[0])})."
        fast = args[1].lower() in ("fast", "priority")
        raw = args[2] if fast and len(args) > 2 else (None if fast else args[1])
        sec = parse_secs(raw) if raw else None
        if sec is None:
            return usage
        if fast:
            if not self.bot.has_fast(store):
                return f"{store.name} has no priority cards; change its full check instead."
            if sec and sec < 10:
                return "Priority checks can't be faster than 10 s."
            self.bot.set_interval(store.id, "fast", sec)
            msg = (f"⚡ {store.name}: priority cards every {fmt_secs(sec)}." if sec else
                   f"⚡ {store.name}: priority checks off (its cards are still checked with the full check).")
        else:
            if sec < 15:
                return "Full checks can't be faster than 15 s."
            self.bot.set_interval(store.id, "full", sec)
            msg = f"⏱ {store.name}: full check every {fmt_secs(sec)}."
        if store.id in RISKY and ((not fast and sec < 60) or (fast and sec and sec < 30)):
            msg += "\n⚠️ That's very frequent; this store may block checks for a while."
        return msg

    # -- WhatsApp / email ----------------------------------------------------
    async def _whatsapp_command(self, client, cid, args: list[str]) -> str:
        ch = self.users.channel(cid, "whatsapp")
        sub = args[0].lower() if args else ""
        if sub in ("on", "off") and ch:
            ch["on"] = sub == "on"
            self._save()
            return f"💬 WhatsApp alerts {'on ✅' if ch['on'] else 'off 🔕'}."
        if sub == "remove" and ch:
            self.users.set_channel(cid, "whatsapp", None)
            self._save()
            return "💬 WhatsApp removed."
        if sub == "test" and ch:
            ok, d = await send_whatsapp(client, ch["phone"], ch["apikey"], "✅ Test from your PSN card bot.")
            return "✅ Test sent, check WhatsApp." if ok else f"❌ Failed: <code>{html.escape(d)}</code>"
        if len(args) >= 2:
            phone, key = clean_phone(args[0]), args[1].strip()
            if not phone:
                return "That phone number doesn't look right. Use e.g. <code>/whatsapp 919876543210 123456</code>"
            ok, d = await send_whatsapp(client, phone, key, "✅ Your PSN card bot will send alerts here.")
            if not ok:
                return (f"❌ WhatsApp test failed: <code>{html.escape(d)}</code>\n"
                        "Check the number (with country code) and the API key from CallMeBot.")
            self.users.set_channel(cid, "whatsapp", {"phone": phone, "apikey": key, "on": True})
            self._save()
            return "✅ WhatsApp linked! You should have a test message. Turn it on/off any time in /menu."
        status = (f"Now: <b>{'on' if ch.get('on', True) else 'off'}</b> for +{ch['phone']}. "
                  "<code>/whatsapp on|off|test|remove</code>\n\n" if ch else "")
        return (f"💬 <b>WhatsApp alerts</b>\n{status}"
                "Set up (free, ~1 minute, via CallMeBot):\n"
                f"1. Save <b>{CALLMEBOT_NUMBER}</b> in your phone contacts.\n"
                "2. On WhatsApp, send it: <code>I allow callmebot to send me messages</code>\n"
                "3. It replies with your API key.\n"
                "4. Send here: <code>/whatsapp YOURNUMBER APIKEY</code> (e.g. <code>/whatsapp 919876543210 123456</code>)\n\n"
                f"<i>If that number doesn't reply, check {CALLMEBOT_HELP} for the current one.</i>\n"
                "You get restock and sold-out alerts there (same stores as /menu).")

    async def _email_command(self, cid, args: list[str]) -> str:
        mailer = self.bot.mailer
        ch = self.users.channel(cid, "email")
        sub = args[0].lower() if args else ""
        if sub in ("on", "off") and ch:
            ch["on"] = sub == "on"
            self._save()
            return f"📧 Email alerts {'on ✅' if ch['on'] else 'off 🔕'}."
        if sub == "remove" and ch:
            self.users.set_channel(cid, "email", None)
            self._save()
            return "📧 Email removed."
        if not mailer.configured:
            if self.users.is_admin(cid):
                return ("📧 Email isn't set up yet. Add these lines to <code>.env</code> and restart the bot:\n"
                        "<code>SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\nSMTP_USER=you@gmail.com\n"
                        "SMTP_PASSWORD=your-16-letter-app-password</code>\n\n"
                        "For Gmail, the password must be an <b>App Password</b> (Google Account → Security → "
                        "2-Step Verification → App passwords), not your normal password.")
            return "📧 Email alerts aren't available yet. Ask the bot's owner to set them up."
        if sub == "test" and ch:
            ok, d = await mailer.send(ch["address"], "✅ Test from your PSN card bot", "Email alerts work.")
            return "✅ Test sent, check your inbox (and spam)." if ok else f"❌ Failed: <code>{html.escape(d)}</code>"
        if args and EMAIL_RE.match(args[0]):
            ok, d = await mailer.send(args[0], "✅ PSN card alerts linked",
                                      "You'll get PlayStation gift card restock alerts here.")
            if not ok:
                return f"❌ Couldn't send to that address: <code>{html.escape(d)}</code>"
            self.users.set_channel(cid, "email", {"address": args[0], "on": True})
            self._save()
            return "✅ Email linked! Check your inbox (and spam) for a test. Turn it on/off any time in /menu."
        status = (f"Now: <b>{'on' if ch.get('on', True) else 'off'}</b> for {html.escape(ch['address'])}. "
                  "<code>/email on|off|test|remove</code>\n\n" if ch else "")
        return (f"📧 <b>Email alerts</b>\n{status}"
                "Send <code>/email you@example.com</code> to get restock and sold-out alerts by email too.")

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
