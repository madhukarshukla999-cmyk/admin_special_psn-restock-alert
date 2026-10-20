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
from . import report
from .users import CARD_VALUES, hm, parse_hm, rule_text
from .channels import CALLMEBOT_HELP, CALLMEBOT_NUMBER, EMAIL_RE, clean_phone, send_whatsapp

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "menu", "description": "My alerts: stores, card values, quiet hours"},
    {"command": "panel", "description": "(owner) Control panel"},
    {"command": "week", "description": "Weekly report"},
    {"command": "quiet", "description": "Quiet hours, e.g. /quiet 23:00 07:00"},
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
    {"command": "extension", "description": "(owner) Connect the laptop browser extension"},
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


def datetime_now() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p")


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
                  f"💳 Card values: <b>{self._values_short(cid)}</b>",
                  f"🌙 Quiet hours: <b>{self._quiet_short(cid)}</b>",
                  f"🔴 Sold-out alerts: <b>{'on' if self.users.pref(cid, 'soldout') else 'off'}</b>",
                  f"📰 Daily summary: <b>{'on' if self.users.pref(cid, 'summary') else 'off'}</b>"
                  f" · 📊 Weekly report: <b>{'on' if self.users.pref(cid, 'weekly') else 'off'}</b>",
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
        rows.append([{"text": f"💳 Card values: {self._values_short(cid, 14)}", "callback_data": "cv:mn:home"},
                     {"text": f"🌙 Quiet: {self._quiet_short(cid)}", "callback_data": "qh:mn:home"}])
        so, su, wk = (self.users.pref(cid, x) for x in ("soldout", "summary", "weekly"))
        rows.append([{"text": f"{'✅' if so else '🔕'} Sold-out", "callback_data": "p:soldout"},
                     {"text": f"{'✅' if su else '🔕'} Daily", "callback_data": "p:summary"},
                     {"text": f"{'✅' if wk else '🔕'} Weekly", "callback_data": "p:weekly"}])
        ch_row = []
        for name, label in (("whatsapp", "WhatsApp"), ("email", "Email")):
            if self.users.channel(cid, name):
                on = self.users.channel_on(cid, name)
                ch_row.append({"text": f"{'✅' if on else '🔕'} {label}", "callback_data": f"p:{name}"})
        if ch_row:
            rows.append(ch_row)
        last = [{"text": "📊 This week", "callback_data": "wk:mn"}, {"text": "🔄 Refresh", "callback_data": "refresh"}]
        if self.users.is_admin(cid):
            last.append({"text": "🎛 Owner panel", "callback_data": "pn:home"})
        rows.append(last)
        return {"inline_keyboard": rows}

    # -- quiet hours / card values: short labels -------------------------
    def _quiet_short(self, cid) -> str:
        q = self.users.quiet(cid)
        return f"{hm(q['start'])}–{hm(q['end'])}" if q else "off"

    def _values_short(self, cid, maxlen: int = 40) -> str:
        rec = self.users.get(cid) or {}
        vals = rec.get("values") or {}
        base = rule_text(vals.get("*"))
        extra = len([k for k in vals if k != "*"])
        txt = base + (f" (+{extra} store rule{'s' if extra > 1 else ''})" if extra else "")
        return txt if len(txt) <= maxlen else txt[:maxlen - 1] + "…"

    # -- quiet hours screen ------------------------------------------------
    QUIET_PRESETS = [(22 * 60, 7 * 60), (23 * 60, 7 * 60), (0, 8 * 60), (1 * 60, 9 * 60)]

    def quiet_view(self, cid, origin: str = "mn") -> tuple[str, dict]:
        q = self.users.quiet(cid)
        raw = (self.users.get(cid) or {}).get("quiet") or {}
        allow = set(raw.get("allow", []))
        if q:
            head = (f"🌙 <b>Quiet hours: {hm(q['start'])} – {hm(q['end'])}</b> (IST)\n\n"
                    "During these hours I won't send restock or sold-out alerts. When they end you get "
                    "<b>one message</b> with everything you missed, and whether it's still in stock.")
            if self.users.in_quiet(cid):
                head += "\n\n😴 <i>Quiet hours are on right now.</i>"
        else:
            head = ("🌙 <b>Quiet hours: off</b>\n\n"
                    "Pick a time to stop alerts at night. You'll get one morning message with what you missed.")
        names = [self.bot.names[s] for s in allow if s in self.bot.names]
        head += ("\n\n🔔 Still alert me at night for: <b>" + ", ".join(names) + "</b>" if names else
                 "\n\n🔔 Tap a store below if you still want its alerts at night (e.g. a store that sells out fast).")
        head += "\n\n<i>Other times: type e.g.</i> <code>/quiet 23:30 06:45</code>"
        o = origin
        cur = (q["start"], q["end"]) if q else None
        presets = [{"text": ("✅ " if cur == (s, e) else "") + f"{hm(s)}–{hm(e)}", "callback_data": f"qh:{o}:set:{s}:{e}"}
                   for s, e in self.QUIET_PRESETS]
        rows = [presets[:2], presets[2:], [{"text": ("✅ " if not q else "") + "🔔 Off – alert me any time", "callback_data": f"qh:{o}:off"}]]
        sb = [{"text": f"{'🔔' if s.id in allow else '🌙'} {s.name}", "callback_data": f"qh:{o}:al:{s.id}"} for s in self.bot.stores]
        rows += [sb[i:i + 2] for i in range(0, len(sb), 2)]
        rows.append(self._back_row(o))
        return head, {"inline_keyboard": rows}

    # -- card values screen ------------------------------------------------
    MIN_PRESETS = [0, 500, 1000, 2000, 3000, 5000]

    def values_view(self, cid, origin: str = "mn") -> tuple[str, dict]:
        base = self.users.value_rule(cid, None)
        lines = ["💳 <b>Which cards do you want alerts for?</b>", "",
                 f"All stores: <b>{rule_text(base)}</b>"]
        for s in self.bot.stores:
            if self.users.has_store_rule(cid, s.id):
                lines.append(f"• {html.escape(s.name)}: <b>{rule_text(self.users.value_rule(cid, s.id))}</b>")
        lines += ["", "Tap a value for <b>all stores</b>, or tap a store to give it its own rule "
                  "(e.g. only ₹1000 on Amazon).", "<i>Cards whose value I can't read are always sent.</i>"]
        o = origin
        cur = base.get("min", 0) if not base.get("only") else None
        mins = [{"text": ("✅ " if cur == v else "") + ("Any value" if not v else f"₹{v:,}+"),
                 "callback_data": f"cv:{o}:all:{v}"} for v in self.MIN_PRESETS]
        rows = [mins[:3], mins[3:]]
        sb = []
        for s in self.bot.stores:
            tag = rule_text(self.users.value_rule(cid, s.id)) if self.users.has_store_rule(cid, s.id) else "same"
            sb.append({"text": f"⚙ {s.name} · {tag}"[:40], "callback_data": f"cv:{o}:st:{s.id}"})
        rows += [sb[i:i + 2] for i in range(0, len(sb), 2)]
        rows.append(self._back_row(o))
        return "\n".join(lines), {"inline_keyboard": rows}

    def values_store_view(self, cid, sid: str, origin: str = "mn") -> tuple[str, dict]:
        name = self.bot.names.get(sid, sid)
        own = self.users.has_store_rule(cid, sid)
        rule = self.users.value_rule(cid, sid)
        lines = [f"💳 <b>{html.escape(name)}</b>: {'<b>' + rule_text(rule) + '</b>' if own else 'same as all stores (' + rule_text(rule) + ')'}",
                 "", "Pick a minimum, or tick the exact values you want (tap again to untick)."]
        o = origin
        only = set(rule.get("only") or []) if own else set()
        cur_min = rule.get("min", 0) if own and not only else None
        rows = [[{"text": ("✅ " if not own else "") + "↩ Same as all stores", "callback_data": f"cv:{o}:sm:{sid}:same"}]]
        mins = [{"text": ("✅ " if cur_min == v else "") + ("Any value" if not v else f"₹{v:,}+"),
                 "callback_data": f"cv:{o}:sm:{sid}:{v}"} for v in self.MIN_PRESETS]
        rows += [mins[:3], mins[3:]]
        exact = [{"text": ("✅ " if v in only else "") + f"₹{v:,}", "callback_data": f"cv:{o}:so:{sid}:{v}"} for v in CARD_VALUES]
        rows += [exact[:4], exact[4:]]
        rows.append([{"text": "⬅ All stores", "callback_data": f"cv:{o}:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _back_row(self, origin: str) -> list[dict]:
        return [{"text": "⬅ Back to panel", "callback_data": "pn:home"}] if origin == "pn" else \
               [{"text": "⬅ Back to my alerts", "callback_data": "refresh"}]

    # -- owner panel -------------------------------------------------------
    async def panel_view(self, client, cid) -> tuple[str, dict]:
        from . import extension_push as ep
        b = self.bot
        st = b.state
        bad = [s.name for s in b.stores if (st.store_errors.get(s.id) or {}).get("count", 0) >= b.error_alert_after]
        stock = sum(1 for r in st.items.values() if r.get("in_stock"))
        up = hist.fmt_duration(time.time() - b.started)
        devs = await ep.devices(client, st)
        online = [d for d in devs if time.time() - d.get("ts", 0) < 600]
        paused = ep.paused(st)
        mode = ep.qr_mode(st)
        mode_txt = {"dm": "📷 QR in DM", "notify": "🔔 pay manually", "off": "⛔ off"}.get(mode, "set per browser")
        bursting = [s.name for s in b.stores if b.burst_active(s)]
        week = hist.recent(st.history, 7 * 86400)
        lines = [f"🎛 <b>Owner panel</b> · <i>{datetime_now()}</i>", "",
                 f"🤖 Bot running for {up} · {len(b.stores) - len(bad)}/{len(b.stores)} stores OK"
                 + (f"\n     ⚠ Can't check: {', '.join(bad)}" if bad else ""),
                 f"📦 In stock right now: <b>{stock}</b> · {len(week)} restocks this week",
                 f"💻 Extension: <b>{'⏸ paused' if paused else '▶ on'}</b> · QR: <b>{mode_txt}</b>"
                 f" · <b>{len(online)}</b> browser{'s' if len(online) != 1 else ''} online",
                 f"⏱ Check speed: " + (f"<b>⚡ burst now</b> ({', '.join(bursting)})" if bursting else "normal"),
                 f"🌙 Your quiet hours: <b>{self._quiet_short(cid)}</b> · 💳 Your cards: <b>{self._values_short(cid, 30)}</b>",
                 "", "Tap a button – everything changes right here."]
        cur = mode or ""
        rows = [
            [{"text": "▶ Turn extension ON" if paused else "⏸ Pause extension", "callback_data": "pn:resume" if paused else "pn:pause"},
             {"text": f"💻 Browsers ({len(online)} online)", "callback_data": "extdv:list"}],
            [{"text": ("✅ " if cur == k else "") + lbl, "callback_data": f"pn:qr:{k}"}
             for k, lbl in (("dm", "📷 QR in DM"), ("notify", "🔔 Pay manually"), ("off", "⛔ QR off"))],
            [{"text": "⏱ Check speed", "callback_data": "cy:list"}, {"text": "🏪 Stores & my alerts", "callback_data": "refresh"}],
            [{"text": "🌙 Quiet hours", "callback_data": "qh:pn:home"}, {"text": "💳 Card values", "callback_data": "cv:pn:home"}],
            [{"text": "📊 Weekly report", "callback_data": "wk:pn"}, {"text": "🔎 Check all now", "callback_data": "pn:check"},
             {"text": "🔄 Refresh", "callback_data": "pn:home"}],
        ]
        return "\n".join(lines), {"inline_keyboard": rows}


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
        rows.append([{"text": "⬅ Back to panel", "callback_data": "pn:home"}])
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
            "/menu: <b>your alerts</b> – stores, which card values (e.g. only ₹2000+), quiet hours at night, "
            "sold-out alerts, daily and weekly reports\n"
            "/week: this week's report\n"
            "/quiet 23:00 07:00: no alerts at night (one morning message instead)\n"
            "/status: what's in stock right now\n"
            "/check: check every store now\n"
            "/history: restocks in the last 7 days\n"
            "/besttime: when each store usually restocks\n"
            "/health: is every store being checked OK\n"
            "/cycles: how often each store is checked\n"
            "/whatsapp, /email: get alerts there too (on/off any time)\n\n"
            "Owner only: <b>/panel</b> (everything in one place), /users, /update, /extension, changing /cycles\n\n"
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
        elif cmd in ("/panel", "/dashboard", "/owner") and admin:
            text, kb = await self.panel_view(client, cid)
            await send(text, reply_markup=kb)
        elif cmd in ("/week", "/weekly", "/report"):
            from . import extension_push as ep
            if admin:
                await ep.refresh_purchases(client, self.bot.state)
            await send(report.weekly_text(self.bot, cid))
        elif cmd in ("/quiet", "/night"):
            if not args:
                text, kb = self.quiet_view(cid)
                await send(text, reply_markup=kb)
            else:
                await send(self._quiet_command(cid, args)[0])
        elif cmd in ("/values", "/cards"):
            text, kb = self.values_view(cid)
            await send(text, reply_markup=kb)
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
        elif cmd == "/extension" and admin:
            text = await self._extension_command(client, args, cid)
            if text:
                await send(text, reply_markup=self._extension_markup())
        elif cmd == "/update" and admin:
            await send("⬆️ Checking for updates…")
            await self.bot.run_update(client, manual_cid=cid)
        elif cmd == "/help":
            await send(self.HELP)
        elif cmd in ("/users", "/update", "/extension", "/panel", "/dashboard", "/owner"):
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
        mid = msg.get("message_id")
        if data.startswith("qh:") or data.startswith("cv:"):
            await self._on_personal_button(client, cq, cid, mid, data)
            return
        if data.startswith("wk:"):
            await self.tg.answer_callback(client, cq["id"], "Weekly report 📊")
            from . import extension_push as ep
            if self.users.is_admin(cid):
                await ep.refresh_purchases(client, self.bot.state)
            back = self._back_row("pn" if data == "wk:pn" else "mn")
            await self.tg.edit(client, cid, mid, report.weekly_text(self.bot, cid), {"inline_keyboard": [back]})
            return
        if data.startswith("pn:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Only the owner can do that")
                return
            await self._on_panel_button(client, cq, cid, mid, data)
            return
        if data.startswith("cy:"):
            await self._on_cycle_button(client, cq, cid, data)
            return
        if data.startswith("extdv:") or data.startswith("extrv:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Only the owner can do that")
                return
            await self._on_devices_button(client, cq, cid, msg, data)
            return
        if data.startswith("extqr:") and data[6:] in ("dm", "notify", "off"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Only the owner can do that")
                return
            await self._set_qr_mode(client, data[6:])
            from . import extension_push as ep
            await self.tg.answer_callback(client, cq["id"], ep.QR_MODES[data[6:]])
            try:
                await self.tg.api(client, "editMessageReplyMarkup", {"chat_id": cid, "message_id": msg.get("message_id"),
                                                                      "reply_markup": self._extension_markup()})
            except Exception:  # noqa: BLE001
                pass
            await self.tg.send(client, self._extension_state_text(), chat_ids=[cid])
            return
        if data in ("ext:on", "ext:off"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Only the owner can do that")
                return
            await self._set_extension_paused(client, data == "ext:off")
            await self.tg.answer_callback(client, cq["id"], "Extension paused ⏸" if data == "ext:off" else "Extension on ▶")
            try:   # keep the message (it may hold the setup code); just swap the button
                await self.tg.api(client, "editMessageReplyMarkup", {"chat_id": cid, "message_id": msg.get("message_id"),
                                                                      "reply_markup": self._extension_markup()})
            except Exception:  # noqa: BLE001
                pass
            await self.tg.send(client, self._extension_state_text(), chat_ids=[cid])
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
        elif data in ("p:soldout", "p:summary", "p:weekly"):
            on = self.users.toggle_pref(cid, data[2:])
            label = {"p:soldout": "Sold-out alerts", "p:summary": "Daily summary", "p:weekly": "Weekly report"}[data]
            note = f"{label}: {'on ✅' if on else 'off 🔕'}"
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

    async def _on_panel_button(self, client, cq, cid, mid, data) -> None:
        from . import extension_push as ep
        note = ""
        if data == "pn:pause":
            await self._set_extension_paused(client, True)
            note = "Extension paused ⏸ – Telegram alerts only"
        elif data == "pn:resume":
            await self._set_extension_paused(client, False)
            note = "Extension on ▶"
        elif data.startswith("pn:qr:") and data[6:] in ep.QR_MODES:
            await self._set_qr_mode(client, data[6:])
            note = ep.QR_MODES[data[6:]]
        elif data == "pn:check":
            wait = 60 - (time.time() - self._last_manual_check)
            if wait > 0:
                note = f"A check just ran – try again in {int(wait)} s"
            else:
                self._last_manual_check = time.time()
                self.bot.request_check(cid)
                note = "Checking every store now… 🔎"
        await self.tg.answer_callback(client, cq["id"], note)
        text, kb = await self.panel_view(client, cid)
        await self.tg.edit(client, cid, mid, text, kb)

    async def _on_personal_button(self, client, cq, cid, mid, data) -> None:
        kind, origin, *rest = data.split(":")
        action = rest[0] if rest else "home"
        note = ""
        if kind == "qh":
            if action == "set" and len(rest) == 3 and rest[1].isdigit() and rest[2].isdigit():
                s, e = int(rest[1]), int(rest[2])
                self.users.set_quiet(cid, s, e)
                note = f"Quiet hours {hm(s)}–{hm(e)} 🌙"
            elif action == "off":
                self.users.set_quiet(cid, None)
                note = "Quiet hours off 🔔"
            elif action == "al" and len(rest) == 2 and rest[1] in self.bot.names:
                on = self.users.toggle_quiet_allow(cid, rest[1])
                note = f"{self.bot.names[rest[1]]}: {'alerts at night too 🔔' if on else 'quiet at night 🌙'}"
            if note:
                self._save()
            text, kb = self.quiet_view(cid, origin)
        else:
            sid = rest[1] if len(rest) > 1 else ""
            if action == "all" and len(rest) == 2 and rest[1].isdigit():
                v = int(rest[1])
                self.users.set_value_rule(cid, "*", {"min": v} if v else None)
                note = "All values" if not v else f"₹{v:,} and above"
            elif action in ("sm", "so") and sid in self.bot.names and len(rest) == 3:
                val = rest[2]
                if action == "sm":
                    if val == "same":
                        self.users.set_value_rule(cid, sid, None)
                        note = f"{self.bot.names[sid]}: same as all stores"
                    elif val.isdigit():
                        self.users.set_value_rule(cid, sid, {"min": int(val)})
                        note = f"{self.bot.names[sid]}: " + rule_text({"min": int(val)})
                elif val.isdigit():
                    cur = self.users.value_rule(cid, sid) if self.users.has_store_rule(cid, sid) else {}
                    only = set(cur.get("only") or [])
                    only ^= {int(val)}
                    self.users.set_value_rule(cid, sid, {"only": sorted(only)} if only else None)
                    note = f"{self.bot.names[sid]}: " + (rule_text({"only": sorted(only)}) if only else "same as all stores")
            if note:
                self._save()
            if action in ("st", "sm", "so") and sid in self.bot.names:
                text, kb = self.values_store_view(cid, sid, origin)
            else:
                text, kb = self.values_view(cid, origin)
        await self.tg.answer_callback(client, cq["id"], note)
        await self.tg.edit(client, cid, mid, text, kb)

    def _quiet_command(self, cid, args: list[str]) -> tuple:
        if args[0].lower() in ("off", "stop", "none"):
            self.users.set_quiet(cid, None)
            self._save()
            return ("🔔 Quiet hours off – I'll alert you any time.",)
        if len(args) >= 2:
            s, e = parse_hm(args[0]), parse_hm(args[-1])
            if s is not None and e is not None and s != e:
                self.users.set_quiet(cid, s, e)
                self._save()
                return (f"🌙 Quiet hours set: <b>{hm(s)} – {hm(e)}</b> IST.\n"
                        "No alerts in that time; one message with what you missed when it ends. "
                        "Change it any time in /menu → 🌙.",)
        return ("Usage: <code>/quiet 23:00 07:00</code> (or <code>/quiet 11pm 7am</code>) · <code>/quiet off</code>",)

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

    def _extension_markup(self) -> dict:
        from . import extension_push as ep
        btn = ({"text": "▶ Turn extension ON", "callback_data": "ext:on"} if ep.paused(self.bot.state)
               else {"text": "⏸ Pause extension (alerts only)", "callback_data": "ext:off"})
        cur = ep.qr_mode(self.bot.state)
        modes = [{"text": ("✅ " if cur == k else "") + {"dm": "📷 QR in DM", "notify": "🔔 Pay manually", "off": "⛔ QR off"}[k],
                  "callback_data": f"extqr:{k}"} for k in ("dm", "notify", "off")]
        return {"inline_keyboard": [[btn], modes, [{"text": "💻 Connected browsers", "callback_data": "extdv:list"}]]}

    async def _devices_view(self, client) -> tuple[str, dict]:
        from . import extension_push as ep
        devs = await ep.devices(client, self.bot.state)
        now = time.time()
        if not devs:
            text = ("💻 <b>Connected browsers</b>\n\nNo browser has reported in the last 12 hours.\n"
                    "<i>Browsers report every few minutes while they're open (needs extension 2.3.0+).</i>")
        else:
            rows = []
            for d in devs:
                ago = now - d.get("ts", 0)
                dot = "🟢" if ago < 10 * 60 else "⚪"
                seen = "now" if ago < 90 else f"{int(ago // 60)} min ago" if ago < 3600 else f"{ago / 3600:.1f} h ago"
                mob = f" · +91 {d['mobile']}" if d.get("mobile") else ""
                state = "⏸ paused" if d.get("paused") else {"dm": "📷 QR in DM", "notify": "🔔 pay manually", "off": "⛔ QR off"}.get(d.get("qr"), "")
                rows.append(f"{dot} <b>{html.escape(d.get('account') or '?')}</b>{html.escape(mob)}\n"
                            f"    {html.escape(d.get('browser') or '?')} · profile {html.escape(d.get('profile') or '?')} · "
                            f"v{html.escape(str(d.get('version') or '?'))}\n    {state} · seen {seen}")
            text = f"💻 <b>Connected browsers ({len(devs)})</b>\n\n" + "\n\n".join(rows)
        kb = [[{"text": f"🚫 Revoke {(d.get('account') or '?')[:14]} · {(d.get('browser') or '?')[:8]} · {(d.get('profile') or '?')[:10]}",
                "callback_data": f"extrv:{d['id'][:40]}"}] for d in devs[:20]]
        kb.append([{"text": "🔄 Refresh", "callback_data": "extdv:list"},
                   {"text": "⛔ Revoke ALL (new code)", "callback_data": "extdv:all?"}])
        kb.append([{"text": "⬅ Back to panel", "callback_data": "pn:home"}])
        return text, {"inline_keyboard": kb}

    def _extension_state_text(self) -> str:
        from . import extension_push as ep
        m = ep.qr_mode(self.bot.state)
        mode = f"\n💳 QR mode: <b>{ep.QR_MODES[m]}</b>" if m else "\n💳 QR mode: <b>set in each browser</b> (choose below to set all)"
        return self._extension_pause_text() + mode

    def _extension_pause_text(self) -> str:
        from . import extension_push as ep
        return ("⏸ <b>Extension paused.</b> Telegram alerts still go to everyone; the laptop "
                "extension won't open tabs, click or send QRs. Tap ▶ to turn it on again."
                if ep.paused(self.bot.state) else
                "▶ <b>Extension ON.</b> Connected browsers open restocks and go to checkout.")

    async def _set_extension_paused(self, client, paused: bool) -> None:
        from . import extension_push as ep
        self.bot.state.settings["ext_paused"] = paused
        self.bot.state.save()
        await ep.publish_control(client, self.bot.state)

    async def _on_devices_button(self, client, cq, cid, msg, data) -> None:
        from . import extension_push as ep
        mid = msg.get("message_id")
        if data == "extdv:all?":
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid, mid,
                               "⛔ <b>Revoke ALL browsers?</b>\n\nEvery connected browser is disconnected and wipes its "
                               "connection. The setup code changes, so old copies can't listen any more. "
                               "You'll need to connect the browsers you keep again with the new code from /extension.",
                               reply_markup={"inline_keyboard": [[{"text": "Yes, revoke all", "callback_data": "extdv:all!"},
                                                                  {"text": "Cancel", "callback_data": "extdv:list"}]]})
            return
        if data == "extdv:all!":
            new = await ep.revoke_all(client, self.bot.state)
            await self.tg.answer_callback(client, cq["id"], "All revoked" if new else "Not possible")
            await self.tg.edit(client, cid, mid,
                               "✅ <b>All browsers revoked.</b> The connection code has changed – send /extension for "
                               "the new setup code and reconnect the browsers you keep.\n\n"
                               "<i>For full safety also change the bot token: @BotFather → /revoke, then put the new "
                               "token in .env and restart the bot.</i>" if new else
                               "❌ The feed is fixed by the NTFY_TOPIC setting, so it can't be changed from here. "
                               "Change NTFY_TOPIC and restart the bot to cut off every browser.")
            return
        if data.startswith("extrv:"):
            dev = data[6:]
            await ep.revoke(client, self.bot.state, dev)
            await self.tg.answer_callback(client, cq["id"], "Revoked 🚫")
        else:
            await self.tg.answer_callback(client, cq["id"])
        text, kb = await self._devices_view(client)
        if data.startswith("extrv:"):
            text = "🚫 Browser revoked – it disconnects and wipes its connection as soon as it's online.\n\n" + text
        try:
            await self.tg.edit(client, cid, mid, text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await self.tg.send(client, text, chat_ids=[cid], reply_markup=kb)

    async def _set_qr_mode(self, client, mode: str) -> None:
        from . import extension_push as ep
        self.bot.state.settings["ext_qr_mode"] = mode
        self.bot.state.save()
        await ep.publish_control(client, self.bot.state)

    async def _extension_command(self, client, args: list[str], cid=None) -> str:
        from . import extension_push as ep
        from .models import Item
        sub = args[0].lower() if args else ""
        if sub in ("off", "pause", "stop"):
            await self._set_extension_paused(client, True)
            return self._extension_state_text()
        if sub in ("on", "resume", "start"):
            await self._set_extension_paused(client, False)
            return self._extension_state_text()
        if sub == "status":
            return self._extension_state_text()
        if sub in ("browsers", "devices", "list"):
            text, kb = await self._devices_view(client)
            await self.tg.send(client, text, chat_ids=[str(cid)], reply_markup=kb)
            return ""
        if sub in ("qr", "mode"):
            want = {"dm": "dm", "photo": "dm", "notify": "notify", "manual": "notify", "off": "off"}.get(args[1].lower() if len(args) > 1 else "")
            if not want:
                return "Usage: <code>/extension qr dm</code> · <code>/extension qr manual</code> · <code>/extension qr off</code>"
            await self._set_qr_mode(client, want)
            return self._extension_state_text()
        if sub == "test" and ep.paused(self.bot.state):
            return "⏸ The extension is paused – tap ▶ below (or send /extension on) first."
        if args and args[0].lower() == "test":
            url = args[1] if len(args) > 1 and args[1].startswith("https://") else "https://www.gamestheshop.com/"
            store = ("amazon" if "amazon." in url else "gamestheshop" if "gamestheshop" in url
                     else "flipkart" if "flipkart" in url else "test")
            ok = await ep.publish(client, self.bot.state, "Test", [Item(store, "TEST", "Test alert", url, True)], test=True)
            return ("✅ Test sent. Your laptop browser should open the page within a few seconds."
                    if ok else "❌ Couldn't reach ntfy.sh; check /health later.")
        import os
        code = ep.setup_code(self.bot.state, cid or "", getattr(self.tg, "token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        return ("💻 <b>Laptop browser extension</b>\n"
                f"{self._extension_state_text()}\n\n"
                "1. Download the code from GitHub (green <b>Code</b> → Download ZIP) and unzip it.\n"
                "2. Open <code>chrome://extensions</code> (or brave://, edge://), turn on <b>Developer mode</b>, "
                "<b>Load unpacked</b> → pick the <code>extension</code> folder. Its setup page opens by itself.\n"
                "3. Paste this <b>setup code</b> there (tap to copy):\n"
                f"<tg-spoiler><code>{html.escape(code)}</code></tg-spoiler>\n"
                "4. Fill in what it asks (browser, profile, account name, mobile) and press <b>Connect</b>. "
                "You'll get a ✅ message here.\n"
                "5. Send <code>/extension test &lt;product link&gt;</code> to try it.\n\n"
                "Pause/resume any time: the button below, or <code>/extension off</code> / <code>/extension on</code>.\n"
                "QR mode for all browsers: the buttons below, or <code>/extension qr dm</code> · <code>manual</code> · <code>off</code>.\n"
                "See or revoke connected browsers: 💻 below, or <code>/extension browsers</code>.\n"
                "<i>⚠ The setup code contains your bot token – don't forward it. Delete this message after setup.</i>")

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
