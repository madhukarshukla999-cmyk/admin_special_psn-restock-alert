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
from .users import CARD_VALUES, PUBLIC_CMDS, hm, parse_hm, rule_text
from .channels import CALLMEBOT_HELP, CALLMEBOT_NUMBER, EMAIL_RE, clean_phone, send_whatsapp

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "menu", "description": "My alerts: stores, card values, quiet hours"},

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

    {"command": "help", "description": "How this bot works"},
]


OWNER_COMMANDS = [
    {"command": "panel", "description": "Control panel"},
    {"command": "groups", "description": "Post alerts to a group / channel"},
    {"command": "users", "description": "Who gets alerts"},
    {"command": "extension", "description": "Connect the laptop browser extension"},
    {"command": "buy", "description": "GamesTheShop auto-buy (accounts, on/off)"},
    {"command": "update", "description": "Install the latest version"},
]
OWNER_ONLY = {"/panel", "/dashboard", "/owner", "/groups", "/group", "/channels", "/users", "/update", "/extension",
              "/buy", "/gtsbuy", "/gts", "/autobuy"}
ALIASES = {"start": "menu", "settings": "menu", "notifications": "menu", "intervals": "cycles",
           "weekly": "week", "report": "week", "night": "quiet", "cards": "values", "best": "besttime"}


def canon(cmd: str) -> str:
    n = cmd.lstrip("/").split("@")[0].lower()
    return ALIASES.get(n, n)
DELAY_PRESETS = [0, 30, 60, 120, 300, 600]


def fmt_delay(sec) -> str:
    sec = int(sec or 0)
    return "instant" if not sec else f"{sec}s" if sec < 60 else f"{sec // 60} min" + (f" {sec % 60}s" if sec % 60 else "")


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
        self._group_cmd_at: dict[str, float] = {}
        self._ask: dict[str, dict] = {}       # chat -> what /buy is waiting for them to type

    def _save(self) -> None:
        self.bot.state.save()

    def _stores(self, cid):
        """Stores this person (or connected group) can see; private ones are owner-only."""
        hide = self.users.hidden(cid)
        out = [s for s in self.bot.stores if s.id not in hide]
        g = self.bot.groups.all.get(str(cid)) if cid else None
        if g:                                     # inside a group: only the stores it posts
            allowed = set(g.get("stores") or [])
            out = [s for s in out if s.id in allowed]
        return out

    def _priv_tag(self, cid, sid) -> str:
        return " 🔒" if self.users.is_admin(cid) and sid in self.users.private() else ""

    # -- views -----------------------------------------------------------
    def _in_stock(self, store_id: str) -> int:
        return sum(1 for r in self.bot.state.items.values() if r.get("store") == store_id and r.get("in_stock"))

    def menu_text(self, cid) -> str:
        muted = self.users.muted(cid)
        lines = ["🛎 <b>Your notifications</b>", ""]
        for s in self._stores(cid):
            on = s.id not in muted
            n = self._in_stock(s.id)
            lines.append(f"{'✅' if on else '🔕'} <b>{html.escape(s.name)}</b>{self._priv_tag(cid, s.id)}: {'on' if on else 'off'}"
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
                   for s in self._stores(cid)]
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
        sb = [{"text": f"{'🔔' if s.id in allow else '🌙'} {s.name}", "callback_data": f"qh:{o}:al:{s.id}"} for s in self._stores(cid)]
        rows += [sb[i:i + 2] for i in range(0, len(sb), 2)]
        rows.append(self._back_row(o))
        return head, {"inline_keyboard": rows}

    # -- card values screen ------------------------------------------------
    MIN_PRESETS = [0, 500, 1000, 2000, 3000, 5000]

    def values_view(self, cid, origin: str = "mn") -> tuple[str, dict]:
        base = self.users.value_rule(cid, None)
        lines = ["💳 <b>Which cards do you want alerts for?</b>", "",
                 f"All stores: <b>{rule_text(base)}</b>"]
        for s in self._stores(cid):
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
        for s in self._stores(cid):
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
                 f"📢 Groups: <b>{len(b.groups.all)}</b> connected" if b.groups.all else "📢 Groups: none connected",
                 f"👥 {len(self.users.active()) - 1} other people"
                 + (f" · 🚫 {len(self.users.blocked())} blocked" if self.users.blocked() else "")
                 + f" · 🔑 {sum(1 for c in PUBLIC_CMDS if self.users.cmd_allowed(c, 'users'))}/{len(PUBLIC_CMDS)} commands on",
                 f"👑 You get alerts first · others wait <b>{fmt_delay((st.settings.get('delays') or {}).get('users', 0))}</b>, "
                 f"groups <b>{fmt_delay((st.settings.get('delays') or {}).get('groups', 0))}</b>"
                 + (f" · 🔒 private: {', '.join(b.names[s] for s in self.users.private() if s in b.names)}" if self.users.private() else ""),
                 "", "Tap a button – everything changes right here."]
        cur = mode or ""
        rows = [
            [{"text": "▶ Turn extension ON" if paused else "⏸ Pause extension", "callback_data": "pn:resume" if paused else "pn:pause"},
             {"text": f"💻 Browsers ({len(online)} online)", "callback_data": "extdv:list"}],
            [{"text": ("✅ " if cur == k else "") + lbl, "callback_data": f"pn:qr:{k}"}
             for k, lbl in (("dm", "📷 QR in DM"), ("notify", "🔔 Pay manually"), ("off", "⛔ QR off"))],
            [{"text": "⏱ Check speed", "callback_data": "cy:list"}, {"text": "🏪 Stores & my alerts", "callback_data": "refresh"}],
            [{"text": "🌙 Quiet hours", "callback_data": "qh:pn:home"}, {"text": "💳 Card values", "callback_data": "cv:pn:home"}],
            [{"text": "🔑 Commands", "callback_data": "cm:home"},
             {"text": "⏳ Alert delays", "callback_data": "dl:home"}],
            [
             {"text": f"🔒 Private stores ({len(self.users.private())})", "callback_data": "pv:home"}],
            [{"text": f"📢 Groups ({len(b.groups.all)})", "callback_data": "gr:list"},
             {"text": "📊 Weekly report", "callback_data": "wk:pn"}],
            [{"text": "🛒 GamesTheShop auto-buy", "callback_data": "gb:home"}],
            [{"text": "🔎 Check all now", "callback_data": "pn:check"}, {"text": "🔄 Refresh", "callback_data": "pn:home"}],
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
        admin = self.users.is_admin(cid)
        for s in self._stores(cid):
            full, fast = b.base_intervals(s)
            dfull, dfast = b.default_intervals(s)
            extra = f" · priority cards every <b>{fmt_secs(fast)}</b>" if b.has_fast(s) else ""
            changed = " ✏️" if (full, fast) != (dfull, dfast) else ""
            lines.append(f"• <b>{html.escape(s.name)}</b>: every <b>{fmt_secs(full)}</b>{extra}{changed}")
            act = b.burst_active(s)
            if act:
                lines.append(f"    ⚡ <b>faster checks now</b> until {act[1] // 60:02d}:{act[1] % 60:02d} IST: every "
                             f"<b>{fmt_secs(b.burst_seconds(s))}</b>" + (f" ({act[2]})" if admin else ""))
            else:
                ws = b.burst_windows(s)
                if ws:
                    lines.append("    ⚡ faster checks " + ", ".join(f"{a // 60:02d}:{a % 60:02d}–{z // 60:02d}:{z % 60:02d}"
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
        delay = self.users.delay(cid)
        lines = ["📦 <b>In stock right now</b>", ""]
        any_stock = False
        for s in self._stores(cid):
            recs = [(k, r) for k, r in self.bot.state.items.items() if r.get("store") == s.id and r.get("in_stock")
                    and time.time() - (r.get("last_change") or 0) >= delay]
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

    def history_text(self, days: int = 7, cid=None) -> str:
        names = {s.id: s.name for s in self._stores(cid)}
        evs = [e for e in hist.recent(self.bot.state.history, days * 86400) if e["store"] in names]
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

    def besttime_text(self, cid=None) -> str:
        names = {s.id: s.name for s in self._stores(cid)}
        lines = hist.best_times([e for e in self.bot.state.history if e["store"] in names], names)
        if not lines:
            return ("⏰ No restocks recorded yet. Once cards come back a few times on different days, "
                    "I'll tell you when each store usually restocks.")
        return "⏰ <b>When stores usually restock</b> (last 30 days)\n\n" + "\n".join(lines) + \
               "\n\n<i>Needs at least 3 restocks on 2+ days per store.</i>"

    def health_text(self, cid=None) -> str:
        b = self.bot
        lines = ["🩺 <b>Health</b>", ""]
        for s in self._stores(cid):
            e = b.state.store_errors.get(s.id) or {}
            if e.get("count"):
                lines.append(f"⚠️ <b>{html.escape(s.name)}</b>: failing ({e['count']} in a row), last OK "
                             f"{_ago(e.get('last_ok'))}\n    <code>{html.escape((e.get('last_error') or '')[:150])}</code>")
            else:
                took = f", took {e['took']:.1f}s" if e.get("took") else ""
                lines.append(f"✅ <b>{html.escape(s.name)}</b>: OK, checked {_ago(e.get('last_ok'))}{took}")
        st = b.state.settings
        if not self.users.is_admin(cid):
            return "\n".join(lines)
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
                rows.append([{"text": f"❌ Remove {rec.get('name') or cid}"[:28], "callback_data": f"u:rm:{cid}"},
                             {"text": "🚫 Block", "callback_data": f"u:bl:{cid}"}])
            elif rec.get("pending"):
                tries = rec.get("tries", 1)
                lines.append(f"⏳ {name} (<code>{cid}</code>) · waiting"
                             + (f" · asked {tries}×" if tries > 1 else ""))
                rows.append([{"text": f"✅ Allow {rec.get('name') or cid}"[:24], "callback_data": f"u:ok:{cid}"},
                             {"text": "❌ Deny", "callback_data": f"u:no:{cid}"},
                             {"text": "🚫 Block", "callback_data": f"u:bl:{cid}"}])
        den = [(c, r) for c, r in self.users.denied().items() if not r.get("blocked")]
        if den:
            lines.append("")
            for cid2, rec in den[:10]:
                lines.append(f"❌ {html.escape(rec.get('name') or cid2)} (<code>{cid2}</code>) · refused"
                             + (f" · asked {rec['tries']}×" if rec.get("tries", 1) > 1 else ""))
                rows.append([{"text": f"✅ Allow {rec.get('name') or cid2}"[:24], "callback_data": f"u:ok:{cid2}"},
                             {"text": "🚫 Block", "callback_data": f"u:bl:{cid2}"}])
        blk = self.users.blocked()
        if blk:
            lines.append("")
            for cid2, rec in list(blk.items())[:10]:
                lines.append(f"🚫 {html.escape(rec.get('name') or cid2)} (<code>{cid2}</code>) · blocked")
                rows.append([{"text": f"♻ Unblock {rec.get('name') or cid2}"[:34], "callback_data": f"u:ub:{cid2}"}])
        lines += ["", "To add someone: they open this bot in Telegram and tap <b>Start</b>. "
                      "You'll get Allow / Deny / Block buttons.",
                  "<i>❌ Deny refuses them but keeps them quiet. 🚫 Block also stops the bot answering "
                  "them at all – use it for repeat requests.</i>"]
        rows.append([{"text": "🔑 Commands", "callback_data": "cm:home"}, {"text": "🔄 Refresh", "callback_data": "u:list"}])
        return "\n".join(lines), {"inline_keyboard": rows}

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
            "\n"
            "Turning a store off only stops its messages; it's still checked in the background.")

    HELP_OWNER = ("\n\n🔐 <b>Only you see this</b>\n"
                  "/panel: everything in one place · /buy: GamesTheShop auto-buy · /groups: post to groups and channels · /users · /extension · /update\n"
                  "🔒 Private stores and ⏳ alert delays (in /panel) are invisible to everyone else.")

    # -- handling --------------------------------------------------------
    async def handle(self, client, upd: dict) -> None:
        if "callback_query" in upd:
            await self._on_button(client, upd["callback_query"])
            return
        if "my_chat_member" in upd:                      # added to / removed from a group or channel
            await self.bot.groups.on_member_update(client, upd["my_chat_member"])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if chat.get("type") in ("group", "supergroup", "channel"):
            if msg.get("migrate_to_chat_id"):            # group upgraded to a supergroup
                await self.bot.groups.migrate(str(cid), str(msg["migrate_to_chat_id"]))
                return
            await self._group_command(client, msg, chat)
            return
        text = (msg.get("text") or "").strip()
        ask = self._ask.get(str(cid)) if cid else None
        if ask and text and not text.startswith("/") and self.users.is_admin(cid):
            if time.time() - ask["ts"] < 600:
                await self._buy_on_text(client, str(cid), text)
                return
            self._ask.pop(str(cid), None)
        if not cid or not text.startswith("/"):
            return
        cid = str(cid)
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        send = lambda t, **kw: self.tg.send(client, t, chat_ids=[cid], **kw)  # noqa: E731

        if self.users.is_blocked(cid):                    # blocked: the bot simply doesn't answer
            return
        name_c = canon(cmd)
        if not self.users.is_allowed(cid):
            if (self.users.get(cid) or {}).get("denied"):   # already refused: don't keep promising
                await send("🔒 You don't have access to this bot.")
                return
            if cmd != "/start":
                await send("🔒 You don't have access yet. Send /start to request it.")
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
                                       {"text": "❌ Deny", "callback_data": f"u:no:{cid}"},
                                       {"text": "🚫 Block", "callback_data": f"u:bl:{cid}"}]]})
            await send("👋 Request sent. You'll get a message once you're approved.")
            return

        admin = self.users.is_admin(cid)
        if not admin and name_c in PUBLIC_CMDS and not self.users.cmd_allowed(name_c, "users"):
            await send("🔒 That command is switched off.")
            return
        if cmd in ("/menu", "/start", "/settings", "/notifications"):
            await send(self.menu_text(cid), reply_markup=self.menu_markup(cid))
        elif cmd in ("/groups", "/group", "/channels") and admin:
            text, kb = await self.bot.groups.list_view(client)
            await send(text, reply_markup=kb)
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
            await send(self.history_text(max(1, min(days, 90)), cid))
        elif cmd in ("/besttime", "/best"):
            await send(self.besttime_text(cid))
        elif cmd == "/health":
            await send(self.health_text(cid))
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
        elif cmd in ("/buy", "/gtsbuy", "/gts", "/autobuy") and admin:
            self._ask.pop(cid, None)
            if not args:
                text, kb = self._buy_view()
                await send(text, reply_markup=kb)
            else:
                await send(await self._gtsbuy_command(client, args, cid))
        elif cmd == "/update" and admin:
            await send("⬆️ Checking for updates…")
            await self.bot.run_update(client, manual_cid=cid)
        elif cmd == "/help":
            await send(self.HELP + (self.HELP_OWNER if admin else ""))
        # owner-only commands from anyone else: no reply, exactly like an unknown command

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
        if data.startswith("gp:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_pay_button(client, cq, cid, data)
            return
        if data.startswith("gb:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_buy_button(client, cq, cid, mid, data)
            return
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
        if data.startswith("gr:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self.bot.groups.on_button(client, cq, cid, mid, data)
            return
        if data.startswith("cm:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_commands_button(client, cq, cid, mid, data)
            return
        if data.startswith("dl:") or data.startswith("pv:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_privilege_button(client, cq, cid, mid, data)
            return
        if data.startswith("pn:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_panel_button(client, cq, cid, mid, data)
            return
        if data.startswith("cy:"):
            await self._on_cycle_button(client, cq, cid, data)
            return
        if data.startswith("extdv:") or data.startswith("extrv:"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            await self._on_devices_button(client, cq, cid, msg, data)
            return
        if data.startswith("xr:"):                  # 🔄 under a QR the browser extension sent
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
                return
            from . import extension_push as ep
            _, dev, tab = (data.split(":", 2) + ["", ""])[:3]
            ok = tab.isdigit() and await ep.publish_action(client, self.bot.state, "refresh", device=dev, tab=int(tab))
            await self.tg.answer_callback(client, cq["id"], "🔄 Refreshing that tab – screenshot follows" if ok
                                          else "Couldn't reach the browser")
            return
        if data.startswith("extqr:") and data[6:] in ("dm", "notify", "off"):
            if not self.users.is_admin(cid):
                await self.tg.answer_callback(client, cq["id"], "Not available")
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
                await self.tg.answer_callback(client, cq["id"], "Not available")
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

        ids = {s.id: s.name for s in self._stores(cid)}
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

    # -- owner privileges: delays + private stores (nobody else can see these) ----
    def delays_view(self) -> tuple[str, dict]:
        d = self.bot.state.settings.get("delays") or {}
        people = [(c, r) for c, r in self.users.active().items() if not r.get("admin")]
        lines = ["⏳ <b>Alert delays</b> 🔐 <i>only you see this</i>", "",
                 "You always get alerts <b>instantly</b>. Everyone else waits this long, and only gets cards that "
                 "are <b>still in stock</b> when the wait is over. Nobody is told there's a delay – /status hides "
                 "new stock from them for the same time.", "",
                 f"👥 Other people: <b>{fmt_delay(d.get('users', 0))}</b>",
                 f"📢 Groups & channels: <b>{fmt_delay(d.get('groups', 0))}</b> <i>(each group can override it in 📢 Groups)</i>"]
        for c, r in people:
            if r.get("delay") is not None:
                lines.append(f"   • {html.escape(r.get('name') or c)}: <b>{fmt_delay(r['delay'])}</b> (own)")
        rows = [[{"text": "👥 People:", "callback_data": "dl:home"}],
                [{"text": ("✅ " if int(d.get("users", 0)) == v else "") + fmt_delay(v), "callback_data": f"dl:u:{v}"} for v in DELAY_PRESETS],
                [{"text": "📢 Groups:", "callback_data": "dl:home"}],
                [{"text": ("✅ " if int(d.get("groups", 0)) == v else "") + fmt_delay(v), "callback_data": f"dl:g:{v}"} for v in DELAY_PRESETS]]
        for c, r in people[:15]:
            rows.append([{"text": f"👤 {r.get('name') or c} · {fmt_delay(self.users.delay(c))}"[:40], "callback_data": f"dl:p:{c}"}])
        rows.append([{"text": "⬅ Back to panel", "callback_data": "pn:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def person_delay_view(self, pid) -> tuple[str, dict]:
        r = self.users.get(pid) or {}
        own = r.get("delay")
        text = (f"⏳ <b>{html.escape(r.get('name') or pid)}</b>\nAlert delay: <b>{fmt_delay(self.users.delay(pid))}</b>"
                + ("" if own is not None else " (same as everyone)"))
        rows = [[{"text": ("✅ " if own is None else "") + "↩ Same as everyone", "callback_data": f"dl:ps:{pid}:d"}],
                [{"text": ("✅ " if own == v else "") + fmt_delay(v), "callback_data": f"dl:ps:{pid}:{v}"} for v in DELAY_PRESETS],
                [{"text": "⬅ All delays", "callback_data": "dl:home"}]]
        return text, {"inline_keyboard": rows}

    def private_view(self) -> tuple[str, dict]:
        priv = self.users.private()
        lines = ["🔒 <b>Private stores</b> 🔐 <i>only you see this</i>", "",
                 "A private store is <b>yours alone</b>: only you get its alerts, and for everyone else it doesn't "
                 "exist – not in /menu, /status, /history, /besttime, /cycles, reports or groups.",
                 "Your browser extension keeps working for it.", "",
                 "Private now: <b>" + (", ".join(self.bot.names[s] for s in priv if s in self.bot.names) or "none") + "</b>"]
        sb = [{"text": f"{'🔒' if s.id in priv else '🌐'} {s.name}", "callback_data": f"pv:t:{s.id}"} for s in self.bot.stores]
        rows = [sb[i:i + 2] for i in range(0, len(sb), 2)]
        rows.append([{"text": "⬅ Back to panel", "callback_data": "pn:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    async def _on_privilege_button(self, client, cq, cid, mid, data) -> None:
        p = data.split(":")
        st = self.bot.state.settings
        note = ""
        if p[0] == "pv":
            if len(p) == 3 and p[1] == "t" and p[2] in self.bot.names:
                priv = self.users.private() ^ {p[2]}
                st["private_stores"] = sorted(priv)
                note = f"{self.bot.names[p[2]]}: {'private 🔒' if p[2] in priv else 'visible to everyone 🌐'}"
            text, kb = self.private_view()
        else:
            if len(p) == 3 and p[1] in ("u", "g") and p[2].isdigit():
                st.setdefault("delays", {})["users" if p[1] == "u" else "groups"] = int(p[2])
                note = f"{'People' if p[1] == 'u' else 'Groups'}: {fmt_delay(int(p[2]))}"
            if len(p) == 3 and p[1] == "p" and self.users.get(p[2]):
                text, kb = self.person_delay_view(p[2])
            elif len(p) == 4 and p[1] == "ps" and self.users.get(p[2]):
                rec = self.users.all[p[2]]
                if p[3] == "d":
                    rec.pop("delay", None)
                elif p[3].isdigit():
                    rec["delay"] = int(p[3])
                note = f"{rec.get('name') or p[2]}: {fmt_delay(self.users.delay(p[2]))}"
                text, kb = self.person_delay_view(p[2])
            else:
                text, kb = self.delays_view()
        if note:
            self._save()
        await self.tg.answer_callback(client, cq["id"], note)
        await self.tg.edit(client, cid, mid, text, kb)

    def commands_view(self) -> tuple[str, dict]:
        lines = ["🔑 <b>Commands</b> 🔐 <i>only you see this</i>", "",
                 "What <b>other people</b> (👤) and <b>connected groups</b> (👥) may use. "
                 "You always have every command.", ""]
        rows = []
        for cmd, (label, in_groups) in PUBLIC_CMDS.items():
            u = self.users.cmd_allowed(cmd, "users")
            g = self.users.cmd_allowed(cmd, "groups")
            lines.append(f"{'✅' if u else '🚫'} {label}" + (f" · 👥 {'on' if g else 'off'}" if in_groups else ""))
            row = [{"text": f"{'✅' if u else '🚫'} 👤 /{cmd}", "callback_data": f"cm:u:{cmd}"}]
            if in_groups:
                row.append({"text": f"{'✅' if g else '🚫'} 👥 group", "callback_data": f"cm:g:{cmd}"})
            rows.append(row)
        lines += ["", "<i>Turned-off commands disappear from their command menu and reply "
                  "\"switched off\" if typed. Only 👥-marked ones can work in groups at all.</i>"]
        rows.append([{"text": "⬅ Back to panel", "callback_data": "pn:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    async def _on_commands_button(self, client, cq, cid, mid, data) -> None:
        p = data.split(":")
        note = ""
        if len(p) == 3 and p[1] in ("u", "g") and p[2] in PUBLIC_CMDS:
            where = "users" if p[1] == "u" else "groups"
            on = not self.users.cmd_allowed(p[2], where)
            self.users.set_cmd(p[2], where, on)
            self._save()
            note = f"/{p[2]} for {'people' if where == 'users' else 'groups'}: {'on ✅' if on else 'off 🚫'}"
            await self._publish_commands(client)
        await self.tg.answer_callback(client, cq["id"], note)
        text, kb = self.commands_view()
        await self.tg.edit(client, cid, mid, text, kb)

    async def _publish_commands(self, client) -> None:
        """Telegram's command menus: public list, group list, and the owner's full list."""
        pub = [c for c in COMMANDS if self.users.cmd_allowed(canon(c["command"]), "users")]
        grp = [c for c in COMMANDS if self.users.cmd_allowed(canon(c["command"]), "groups")]
        await self.tg.api(client, "setMyCommands", {"commands": pub})
        await self.tg.api(client, "setMyCommands", {"commands": grp, "scope": {"type": "all_group_chats"}})
        for a in self.users.admins:
            await self.tg.api(client, "setMyCommands", {
                "commands": OWNER_COMMANDS[:1] + COMMANDS + OWNER_COMMANDS[1:],
                "scope": {"type": "chat", "chat_id": int(a) if a.lstrip("-").isdigit() else a}})

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
            await self.tg.answer_callback(client, cq["id"], "Not available")
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
            return "See how often each store is checked with /cycles."
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
            return "📧 Email alerts aren't available yet."
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

    DEV_PER_PAGE = 10        # a fleet can be 50+ browsers, so the list is paged

    async def _devices_view(self, client, page: int = 0) -> tuple[str, dict]:
        from . import extension_push as ep
        devs = await ep.devices(client, self.bot.state)
        now = time.time()
        online = sum(1 for d in devs if now - d.get("ts", 0) < 10 * 60)
        per = self.DEV_PER_PAGE
        pages = max(1, -(-len(devs) // per))
        page = max(0, min(page, pages - 1))
        shown = devs[page * per:(page + 1) * per]
        if not devs:
            text = ("💻 <b>Connected browsers</b>\n\nNo browser has reported in the last 6 hours.\n"
                    "<i>Browsers report every ~15 min while they're open (needs extension 2.3.0+).</i>")
        else:
            rows = []
            for d in shown:
                ago = now - d.get("ts", 0)
                dot = "🟢" if ago < 10 * 60 else "⚪"
                seen = "now" if ago < 90 else f"{int(ago // 60)} min ago" if ago < 3600 else f"{ago / 3600:.1f} h ago"
                mob = f" · +91 {d['mobile']}" if d.get("mobile") else ""
                state = "⏸ paused" if d.get("paused") else {"dm": "📷 QR in DM", "notify": "🔔 pay manually", "off": "⛔ QR off"}.get(d.get("qr"), "")
                rows.append(f"{dot} <b>{html.escape(d.get('account') or '?')}</b>{html.escape(mob)}\n"
                            f"    {html.escape(d.get('browser') or '?')} · profile {html.escape(d.get('profile') or '?')} · "
                            f"v{html.escape(str(d.get('version') or '?'))}\n    {state} · seen {seen}")
            head = f"💻 <b>Connected browsers ({len(devs)}, {online} online)</b>"
            if pages > 1:
                head += f" · page {page + 1}/{pages}"
            text = head + "\n\n" + "\n\n".join(rows)
        kb = [[{"text": f"🚫 Revoke {(d.get('account') or '?')[:14]} · {(d.get('browser') or '?')[:8]} · {(d.get('profile') or '?')[:10]}",
                "callback_data": f"extrv:{d['id'][:40]}"}] for d in shown]
        if pages > 1:
            nav = []
            if page:
                nav.append({"text": "◀ Previous", "callback_data": f"extdv:p:{page - 1}"})
            if page < pages - 1:
                nav.append({"text": "Next ▶", "callback_data": f"extdv:p:{page + 1}"})
            kb.append(nav)
        kb.append([{"text": "🔄 Refresh", "callback_data": f"extdv:p:{page}"},
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
        if data.startswith("extdv:p:"):
            await self.tg.answer_callback(client, cq["id"])
            text, kb = await self._devices_view(client, int(data.rsplit(":", 1)[1] or 0))
            await self.tg.edit(client, cid, mid, text, kb)
            return
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

    # -- /buy: GamesTheShop auto-buy, all with buttons -----------------------------------------
    def _buy_view(self, note: str = "") -> tuple[str, dict]:
        from . import gtscheckout as gc
        st = self.bot.state
        accts = gc.accounts(st)
        on = gc.auto_on(st)
        logged = [l for l, a in accts.items() if a.get("access")]
        lines = ["🛒 <b>GamesTheShop auto-buy</b>", ""]
        if note:
            lines += [note, ""]
        lines.append(f"Status: <b>{'🟢 ON' if on else '⏸ OFF'}</b>")
        lines.append("When PlayStation cards come back, each account orders one of every card, and the "
                     f"payment QRs arrive here. Each QR is deleted after {gc.QR_MINUTES} min. "
                     "Nothing is paid until you scan one.")
        lines += ["", "<b>Accounts</b>"]
        if not accts:
            lines.append("None yet. Tap ➕ Add account.")
        for label, a in accts.items():
            state = "⏳ waiting for OTP" if a.get("pending_otp") and not a.get("access") else \
                    "🔑 login expired – tap ⚙️ to log in again" if a.get("expired") else \
                    "✅ ready" if a.get("access") else "❌ not logged in"
            lines.append(f"• {html.escape(label)} — {state} · {self._acc_pay_short(label)}")
        if on and not logged:
            lines += ["", "⚠️ Auto-buy is on, but no account is logged in yet."]
        last = st.settings.get("gts_last_run")
        if last:
            cards = ", ".join(last.get("cards") or [])[:120] or "?"
            lines += ["", f"<b>Last restock</b> ({_ago(last.get('ts'))}): {html.escape(cards)}"]
            if last.get("off"):
                lines.append("⏸ Auto-buy was OFF, so nothing was ordered.")
            else:
                lines.append(f"🛒 {last.get('accounts', 0)} account(s) · ✅ {last.get('ok', 0)} order(s) placed"
                             + (f" · ❌ {len(last.get('errors') or [])} problem(s)" if last.get("errors") else ""))
                for e in (last.get("errors") or [])[:4]:
                    lines.append(f"   • {html.escape(e)[:160]}")
        vpa, mode = gc.upi_id(st), gc.pay_mode(st)
        lines += ["", "<b>Payment – default for all accounts</b> (tap ⚙️ on an account to change just that one)",
                  ("📲 A payment request goes to <b>" + html.escape(vpa) + "</b>; the QR comes too as a backup."
                   if mode == "upi" else "📷 You get a QR to scan" + (f" (UPI ID {html.escape(vpa)} saved for the 📲 button)." if vpa else "."))]
        open_n = len(gc.PENDING)
        if open_n:
            lines.append(f"🧾 {open_n} payment{'s' if open_n != 1 else ''} waiting.")
        rows = [[{"text": "⏸ Turn auto-buy OFF" if on else "🟢 Turn auto-buy ON", "callback_data": "gb:tog"}],
                [{"text": "💳 Default: " + ("📲 UPI request" if mode == "upi" else "📷 QR"), "callback_data": "gb:pm"},
                 {"text": "✏️ Default UPI ID" + (f": {vpa}" if vpa else ""), "callback_data": "gb:upi"}],
                [{"text": "➕ Add account", "callback_data": "gb:add"}]]
        if open_n:
            rows.append([{"text": f"❌ Cancel all waiting payments ({open_n})", "callback_data": "gb:xall"}])
        links = self._test_links()
        if links:
            rows.append([{"text": "🧪 Test: 1 order", "callback_data": "gb:t?:1"}]
                        + ([{"text": f"🧪 Test: {len(links)} orders", "callback_data": f"gb:t?:{len(links)}"}]
                           if len(links) > 1 else []))
        rows.append([{"text": "🔗 Test another link", "callback_data": "gb:test"}])
        acc_btns = [{"text": f"⚙️ {label}"[:40], "callback_data": f"gb:acc:{label}"[:64]} for label in list(accts)[:8]]
        rows += [acc_btns[i:i + 2] for i in range(0, len(acc_btns), 2)]
        if logged:
            rows.append([{"text": "🔍 Check logins now", "callback_data": "gb:chk"}])
        rows.append([{"text": "🔄 Refresh", "callback_data": "gb:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _acc_pay_short(self, label: str) -> str:
        from . import gtscheckout as gc
        st = self.bot.state
        own = (gc.accounts(st).get(label) or {})
        mode, vpa = gc.pay_mode(st, label), gc.upi_id(st, label)
        txt = f"📲 UPI {vpa}" if mode == "upi" else "📷 QR"
        return txt + ("" if own.get("pay_mode") in ("qr", "upi") or own.get("upi_id") else " (default)")

    def _buy_acc_view(self, label: str, note: str = "") -> tuple[str, dict]:
        """⚙️ one account: its own UPI ID and payment method (or the defaults), remove."""
        from . import gtscheckout as gc
        st = self.bot.state
        a = gc.accounts(st).get(label) or {}
        own_vpa, own_mode = str(a.get("upi_id") or ""), a.get("pay_mode")
        mode, vpa = gc.pay_mode(st, label), gc.upi_id(st, label)
        state = ("🔑 expired – log in again" if a.get("expired") else "✅ logged in" if a.get("access")
                 else "⏳ waiting for OTP" if a.get("pending_otp") else "❌ not logged in")
        if a.get("checked") and not a.get("expired"):
            state += f" (checked {_ago(a['checked'])})"
        lines = [f"⚙️ <b>Account {html.escape(label)}</b>", ""]
        if note:
            lines += [note, ""]
        lines += [f"Login: {state}",
                  "UPI ID: " + (f"<b>{html.escape(own_vpa)}</b> (this account)" if own_vpa else
                                (f"{html.escape(vpa)} (default)" if vpa else "none")),
                  "Pay by: " + {"qr": "📷 QR", "upi": "📲 UPI request"}.get(own_mode, "default") +
                  f" → now <b>{'📲 request to ' + html.escape(vpa) if mode == 'upi' else '📷 QR'}</b>"]
        if own_mode == "upi" and not vpa:
            lines.append("⚠️ UPI request needs a UPI ID, so this account gets a QR until you set one.")
        nxt = {None: "qr", "qr": "upi", "upi": None}[own_mode if own_mode in ("qr", "upi") else None]
        nxt_txt = {"qr": "📷 QR", "upi": "📲 UPI request", None: "default"}[nxt]
        rows = [[{"text": f"💳 Pay by: {({'qr': '📷 QR', 'upi': '📲 UPI request'}.get(own_mode, 'default'))} → {nxt_txt}",
                  "callback_data": f"gb:apm:{label}"[:64]}],
                [{"text": "✏️ UPI ID for this account", "callback_data": f"gb:aupi:{label}"[:64]}]]
        if own_vpa:
            rows.append([{"text": "↩ Use the default UPI ID", "callback_data": f"gb:aupirm:{label}"[:64]}])
        rows.append([{"text": "🔑 Log in again (OTP)", "callback_data": f"gb:relogin:{label}"[:64]}])
        rows += [[{"text": f"🗑 Remove {label}"[:40], "callback_data": f"gb:rm?:{label}"[:64]}],
                 [{"text": "⬅ All accounts", "callback_data": "gb:home"}]]
        return "\n".join(lines), {"inline_keyboard": rows}

    def _test_links(self) -> list[str]:
        """Saved test links (config.yaml gts_test_links), so tests need no typing."""
        return [str(u).strip() for u in (self.bot.cfg.get("gts_test_links") or []) if "gamestheshop.com" in str(u)]

    async def _run_saved_test(self, client, cid: str, links: list[str]) -> None:
        from . import gtscheckout as gc
        n_acc = sum(1 for a in gc.accounts(self.bot.state).values() if a.get("access"))
        await self.tg.send(client, f"⏳ Placing test orders: {n_acc} account{'s' if n_acc != 1 else ''} × "
                                   f"{len(links)} card{'s' if len(links) != 1 else ''}. "
                                   "Each QR arrives as soon as it's ready…", chat_ids=[cid])
        result = await self._gtsbuy_command(client, ["test", *links], cid)
        await self.tg.send(client, result, chat_ids=[cid],
                           reply_markup={"inline_keyboard": [[{"text": "⬅ Back to auto-buy", "callback_data": "gb:home"}]]})

    def _ask_markup(self, extra: list | None = None) -> dict:
        return {"inline_keyboard": ([extra] if extra else []) + [[{"text": "✖ Cancel", "callback_data": "gb:cancel"}]]}

    async def _on_buy_button(self, client, cq, cid: str, mid, data: str) -> None:
        from . import gtscheckout as gc
        st = self.bot.state
        accts = gc.accounts(st)
        parts = data.split(":", 2)
        act, arg = parts[1], (parts[2] if len(parts) > 2 else "")
        note = ""
        if act == "tog":
            st.settings["gts_auto"] = not gc.auto_on(st)
            st.save()
            note = ("🟢 Auto-buy is <b>ON</b>. Tip: switch GamesTheShop off in the laptop extension, "
                    "or you'll get orders from both." if gc.auto_on(st) else "⏸ Auto-buy is <b>OFF</b>.")
            await self.tg.answer_callback(client, cq["id"], "Auto-buy " + ("ON" if gc.auto_on(st) else "OFF"))
        elif act == "add":
            self._ask[cid] = {"kind": "mobile", "ts": time.time()}
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid, mid, "📱 <b>Add a GamesTheShop account</b>\n\n"
                               "Type the account's <b>mobile number</b> (10 digits) <b>or email address</b> here and send it.\n"
                               "GamesTheShop sends an OTP by SMS or by email.", self._ask_markup())
            return
        elif act == "relogin" and arg in accts:
            mobile = gc.login_id(accts[arg]) or (arg if arg.isdigit() or "@" in arg else "")
            if not mobile:
                await self.tg.answer_callback(client, cq["id"], "No mobile saved – remove and add it again")
                return
            try:
                await gc.request_otp(client, mobile)
            except gc.GtsError as e:
                await self.tg.answer_callback(client, cq["id"], str(e)[:190])
                return
            self._ask[cid] = {"kind": "otp", "label": arg, "ts": time.time(), "tries": 0}
            await self.tg.answer_callback(client, cq["id"], "OTP sent 📲")
            await self.tg.send(client, f"📲 OTP sent to <b>{html.escape(mobile)}</b> for <b>{html.escape(arg)}</b>.\n\n"
                                       f"Type the <b>OTP</b> from the {'email' if '@' in mobile else 'SMS'} here and send it.", chat_ids=[cid],
                               reply_markup=self._ask_markup([{"text": "🔁 Send OTP again", "callback_data": f"gb:resend:{arg}"[:64]}]))
            return
        elif act == "chk":
            await self.tg.answer_callback(client, cq["id"], "Checking logins…")
            res = await gc.check_logins(self.bot, client)
            word = {"ok": "✅ OK", "expired": "🔑 expired", "unknown": "❔ couldn't check (try again later)"}
            note = "<b>Login check</b>\n" + ("\n".join(f"• {html.escape(k)}: {word.get(v, v)}" for k, v in res.items())
                                             or "No logged-in accounts.")
        elif act in ("acc", "apm", "aupi", "aupirm") and arg in accts:
            a = accts[arg]
            if act == "aupi":
                self._ask[cid] = {"kind": "upi", "label": arg, "ts": time.time()}
                await self.tg.answer_callback(client, cq["id"])
                await self.tg.edit(client, cid, mid, f"✏️ <b>UPI ID for {html.escape(arg)}</b>"
                                   + (f"\nNow: <code>{html.escape(a['upi_id'])}</code>" if a.get("upi_id") else "") +
                                   "\n\nType the UPI ID for this account (e.g. <code>name@okicici</code>) and send it.",
                                   self._ask_markup())
                return
            note = ""
            if act == "apm":
                cur = a.get("pay_mode") if a.get("pay_mode") in ("qr", "upi") else None
                nxt = {None: "qr", "qr": "upi", "upi": None}[cur]
                if nxt:
                    a["pay_mode"] = nxt
                else:
                    a.pop("pay_mode", None)
                st.save()
                note = "Saved."
                if nxt == "upi" and not gc.upi_id(st, arg):
                    note = "⚠️ Set a UPI ID for this account (or a default one) for UPI requests to work."
            elif act == "aupirm":
                a.pop("upi_id", None)
                st.save()
                note = "↩ This account now uses the default UPI ID."
            await self.tg.answer_callback(client, cq["id"], "Saved" if act != "acc" else "")
            text, kb = self._buy_acc_view(arg, note)
            await self.tg.edit(client, cid, mid, text, kb)
            return
        elif act == "pm":
            if not gc.upi_id(st):
                self._ask[cid] = {"kind": "upi", "ts": time.time(), "then_mode": True}
                await self.tg.answer_callback(client, cq["id"])
                await self.tg.edit(client, cid, mid, "💳 <b>Pay by UPI request</b>\n\nType your <b>UPI ID</b> "
                                   "(e.g. <code>name@okicici</code>) and send it. Each order then sends a payment "
                                   "request to your UPI app; you approve it with your PIN.", self._ask_markup())
                return
            st.settings["gts_pay_mode"] = "qr" if gc.pay_mode(st) == "upi" else "upi"
            st.save()
            note = ("📲 Orders now send a payment request to " + html.escape(gc.upi_id(st)) + " (QR as a backup)."
                    if gc.pay_mode(st) == "upi" else "📷 Orders now send a QR.")
            await self.tg.answer_callback(client, cq["id"], "Saved")
        elif act == "upi":
            self._ask[cid] = {"kind": "upi", "ts": time.time()}
            await self.tg.answer_callback(client, cq["id"])
            cur = gc.upi_id(st)
            extra = [{"text": "🗑 Remove UPI ID", "callback_data": "gb:upirm"}] if cur else None
            await self.tg.edit(client, cid, mid, "✏️ <b>UPI ID</b>" + (f"\nNow: <code>{html.escape(cur)}</code>" if cur else "") +
                               "\n\nType your UPI ID (e.g. <code>name@okicici</code>) and send it.", self._ask_markup(extra))
            return
        elif act == "upirm":
            self._ask.pop(cid, None)
            st.settings.pop("gts_upi_id", None)
            st.settings["gts_pay_mode"] = "qr"
            st.save()
            note = "🗑 UPI ID removed. Orders send a QR."
            await self.tg.answer_callback(client, cq["id"], "Removed")
        elif act == "xall":
            n = 0
            for key in list(gc.PENDING):
                if await gc.cancel_payment(self.bot, client, key):
                    n += 1
            note = f"❌ Cancelled {n} waiting payment{'s' if n != 1 else ''}. The unpaid orders expire by themselves."
            await self.tg.answer_callback(client, cq["id"], "Cancelled")
        elif act in ("t?", "t!"):
            links = self._test_links()
            n = int(arg) if arg.isdigit() else 1
            links = links[:max(1, n)]
            n_acc = sum(1 for a in accts.values() if a.get("access"))
            if not links or not n_acc:
                await self.tg.answer_callback(client, cq["id"], "Add an account first" if not n_acc else "No saved test links")
                return
            if act == "t?":                         # one tap = real orders, so confirm first
                await self.tg.answer_callback(client, cq["id"])
                total = n_acc * len(links)
                await self.tg.edit(client, cid, mid,
                                   f"🧪 <b>Test: {len(links)} order{'s' if len(links) != 1 else ''} per account</b>\n\n"
                                   f"This places <b>{total} real order{'s' if total != 1 else ''}</b> "
                                   f"({n_acc} account{'s' if n_acc != 1 else ''} × {len(links)}) and sends "
                                   f"{'their QRs' if total != 1 else 'its QR'} here. Only scan a QR if you want to buy; "
                                   "unpaid orders expire by themselves.",
                                   {"inline_keyboard": [[{"text": "✅ Yes, place them", "callback_data": f"gb:t!:{len(links)}"},
                                                         {"text": "✖ Cancel", "callback_data": "gb:home"}]]})
                return
            await self.tg.answer_callback(client, cq["id"], "Placing test orders…")
            text, kb = self._buy_view("🧪 Test running – QRs arrive below.")
            await self.tg.edit(client, cid, mid, text, kb)
            task = asyncio.create_task(self._run_saved_test(client, cid, links))   # don't block other updates
            self.bot._bg.add(task)
            task.add_done_callback(self.bot._bg.discard)
            return
        elif act == "test":
            if not any(a.get("access") for a in accts.values()):
                await self.tg.answer_callback(client, cq["id"], "Add an account first", )
                return
            self._ask[cid] = {"kind": "test", "ts": time.time()}
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid, mid, "🧪 <b>Test</b>\n\nPaste one or more GamesTheShop card links here "
                               "(one message, links separated by spaces or new lines).\n\n"
                               "⚠️ <b>This places real orders</b> and sends their QRs. Only scan a QR if you want "
                               "to buy. Unpaid orders expire by themselves.", self._ask_markup())
            return
        elif act == "resend" and arg in accts:
            try:
                await gc.request_otp(client, gc.login_id(accts[arg]))
                await self.tg.answer_callback(client, cq["id"], "OTP sent again 📲")
            except gc.GtsError as e:
                await self.tg.answer_callback(client, cq["id"], str(e)[:190])
            self._ask[cid] = {"kind": "otp", "label": arg, "ts": time.time(), "tries": 0}
            return
        elif act == "rm?" and arg in accts:
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid, mid, f"🗑 Remove <b>{html.escape(arg)}</b> from auto-buy?\n"
                               "Its saved login is deleted from the bot.",
                               {"inline_keyboard": [[{"text": "Yes, remove", "callback_data": f"gb:rm!:{arg}"[:64]},
                                                     {"text": "Cancel", "callback_data": f"gb:acc:{arg}"[:64]}]]})
            return
        elif act == "rm!" and arg in accts:
            accts.pop(arg)
            st.save()
            note = f"🗑 Removed {html.escape(arg)}."
            await self.tg.answer_callback(client, cq["id"], "Removed")
        else:                                   # home / cancel / refresh
            self._ask.pop(cid, None)
            await self.tg.answer_callback(client, cq["id"])
        text, kb = self._buy_view(note)
        await self.tg.edit(client, cid, mid, text, kb)

    async def _on_pay_button(self, client, cq, cid: str, data: str) -> None:
        """❌ Cancel / 📲 Send to my UPI ID, under a payment QR."""
        from . import gtscheckout as gc
        _, act, key = (data.split(":", 2) + ["", ""])[:3]
        if act == "x":
            p = await gc.cancel_payment(self.bot, client, key)
            if not p:
                await self.tg.answer_callback(client, cq["id"], "Already gone (paid, cancelled or expired)")
                return
            await self.tg.answer_callback(client, cq["id"], "Cancelled")
            await self.tg.send(client, f"❌ Cancelled: {html.escape(p['label'])} · {html.escape(p['title'])}. "
                                       "Nothing was paid; the unpaid order expires by itself.", chat_ids=[cid])
        elif act == "upi":
            try:
                vpa = await gc.request_payment(self.bot, key)
            except gc.GtsError as e:
                await self.tg.answer_callback(client, cq["id"], str(e)[:190])
                return
            await self.tg.answer_callback(client, cq["id"], f"Request sent to {vpa}")
            await self.tg.send(client, f"📲 Payment request sent to <b>{html.escape(vpa)}</b> – approve it in your "
                                       "UPI app.", chat_ids=[cid])
        elif act == "chk":
            await self.tg.answer_callback(client, cq["id"], "Checking with GamesTheShop…")
            try:
                await gc.check_order_now(self.bot, client, key, [cid])
            except gc.GtsError as e:
                await self.tg.send(client, f"❔ {html.escape(str(e))}", chat_ids=[cid])
        else:
            await self.tg.answer_callback(client, cq["id"])

    async def _buy_on_text(self, client, cid: str, text: str) -> None:
        """The owner typed an answer for /buy: a mobile number, an OTP, or links to test."""
        from . import gtscheckout as gc
        import re as _re
        st = self.bot.state
        accts = gc.accounts(st)
        ask = self._ask[cid]
        send = lambda t, **kw: self.tg.send(client, t, chat_ids=[cid], **kw)  # noqa: E731

        if ask["kind"] == "mobile" and "@" in text:          # email login
            email = text.strip().lower()
            if not _re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
                await send("That doesn't look like an email address, e.g. <code>you@gmail.com</code>.", reply_markup=self._ask_markup())
                return
            try:
                await gc.request_otp(client, email)
            except gc.GtsError as e:
                await send(f"❌ {html.escape(str(e))}\nCheck the email and send it again.", reply_markup=self._ask_markup())
                return
            accts[email] = {**accts.get(email, {}), "email": email, "pending_otp": True}
            st.save()
            self._ask[cid] = {"kind": "otp", "label": email, "ts": time.time(), "tries": 0}
            await send(f"📧 OTP sent to <b>{html.escape(email)}</b>.\n\nType the <b>OTP</b> from the email here and send it "
                       "(check Spam too).",
                       reply_markup=self._ask_markup([{"text": "🔁 Send OTP again", "callback_data": f"gb:resend:{email}"[:64]}]))
            return

        if ask["kind"] == "mobile":
            digits = _re.sub(r"\D", "", text)
            mobile = digits[-10:] if len(digits) >= 10 else ""
            if not _re.fullmatch(r"[6-9]\d{9}", mobile):
                await send("That doesn't look like a mobile number. Type the 10 digits, e.g. <code>9876543210</code>.",
                           reply_markup=self._ask_markup())
                return
            try:
                await gc.request_otp(client, mobile)
            except gc.GtsError as e:
                await send(f"❌ {html.escape(str(e))}\nCheck the number and send it again.", reply_markup=self._ask_markup())
                return
            accts[mobile] = {**accts.get(mobile, {}), "mobile": mobile, "pending_otp": True}
            st.save()
            self._ask[cid] = {"kind": "otp", "label": mobile, "ts": time.time(), "tries": 0}
            await send(f"📲 OTP sent to <b>{mobile}</b>.\n\nType the <b>OTP</b> from the SMS here and send it.",
                       reply_markup=self._ask_markup([{"text": "🔁 Send OTP again", "callback_data": f"gb:resend:{mobile}"}]))
            return

        if ask["kind"] == "otp":
            label = ask["label"]
            otp = gc.clean_otp(text)
            if not 4 <= len(otp) <= 10:
                await send("Type just the code from the SMS / email, e.g. <code>123456</code> or <code>A1B2C3</code>.", reply_markup=self._ask_markup())
                return
            try:
                await gc.verify_otp(client, st, label, gc.login_id(accts[label]), otp)
            except (gc.GtsError, KeyError) as e:
                ask["tries"] = ask.get("tries", 0) + 1
                if ask["tries"] >= 3:
                    self._ask.pop(cid, None)
                    t, kb = self._buy_view(f"❌ Login failed 3 times ({html.escape(str(e))}). Tap ➕ Add account to start again.")
                    await send(t, reply_markup=kb)
                    return
                await send(f"❌ {html.escape(str(e))}\nType the OTP again.",
                           reply_markup=self._ask_markup([{"text": "🔁 Send OTP again", "callback_data": f"gb:resend:{label}"}]))
                return
            self._ask.pop(cid, None)
            t, kb = self._buy_view(f"✅ <b>{html.escape(label)}</b> is logged in.")
            await send(t, reply_markup=kb)
            return

        if ask["kind"] == "upi":
            vpa = text.strip().replace(" ", "")
            if not _re.fullmatch(r"[A-Za-z0-9._\-]{2,256}@[A-Za-z][A-Za-z0-9.\-]{1,63}", vpa):
                await send("That doesn't look like a UPI ID. It looks like <code>name@okicici</code> or "
                           "<code>9876543210@ybl</code>.", reply_markup=self._ask_markup())
                return
            if ask.get("label") and ask["label"] in accts:          # this account only
                accts[ask["label"]]["upi_id"] = vpa
                st.save()
                self._ask.pop(cid, None)
                t, kb = self._buy_acc_view(ask["label"], f"✅ UPI ID saved for this account: <b>{html.escape(vpa)}</b>")
                await send(t, reply_markup=kb)
                return
            st.settings["gts_upi_id"] = vpa
            if ask.get("then_mode"):
                st.settings["gts_pay_mode"] = "upi"
            st.save()
            self._ask.pop(cid, None)
            t, kb = self._buy_view(f"✅ UPI ID saved: <b>{html.escape(vpa)}</b>"
                                   + (". Orders now send a payment request to it." if gc.pay_mode(st) == "upi" else
                                      ". Tap 💳 Pay by to switch to UPI requests, or use 📲 under any QR."))
            await send(t, reply_markup=kb)
            return

        if ask["kind"] == "test":
            links = _re.findall(r"https?://\S*gamestheshop\.com\S*", text)
            if not links:
                await send("I didn't find a GamesTheShop link. Paste the card's link from the browser.",
                           reply_markup=self._ask_markup())
                return
            self._ask.pop(cid, None)
            n_acc = sum(1 for a in accts.values() if a.get("access"))
            await send(f"⏳ Placing test orders: {n_acc} account{'s' if n_acc != 1 else ''} × {len(links)} card"
                       f"{'s' if len(links) != 1 else ''}. Each QR arrives as soon as it's ready…")
            result = await self._gtsbuy_command(client, ["test", *links], cid)
            await send(result, reply_markup={"inline_keyboard": [[{"text": "⬅ Back to auto-buy", "callback_data": "gb:home"}]]})

    async def _gtsbuy_command(self, client, args: list[str], cid=None) -> str:
        """Manage GamesTheShop checkout accounts and run a dry-run checkout (owner only)."""
        from . import gtscheckout as gc
        from .models import Item
        import re as _re

        accts = gc.accounts(self.bot.state)
        sub = args[0].lower() if args else ""

        def acc_list() -> str:
            if not accts:
                return "No accounts yet."
            out = []
            for label, a in accts.items():
                state = "⏳ waiting for OTP" if a.get("pending_otp") else ("✅ logged in" if a.get("access") else "not logged in")
                out.append(f"• <b>{html.escape(label)}</b> — {state}")
            return "\n".join(out)

        if sub in ("add", "login"):
            if len(args) < 2:
                return "Usage: <code>/gtsbuy add &lt;mobile or email&gt;</code> (your GamesTheShop login)"
            if "@" in args[1]:
                mobile = args[1].strip().lower()
            else:
                mobile = _re.sub(r"\D", "", args[1])[-10:]
                if len(mobile) != 10:
                    return "That doesn't look like a 10-digit mobile number or an email."
            label = args[2] if len(args) > 2 else mobile
            try:
                await gc.request_otp(client, mobile)
            except gc.GtsError as e:
                return f"❌ {html.escape(str(e))}"
            accts[label] = {**accts.get(label, {}), ("email" if "@" in mobile else "mobile"): mobile, "pending_otp": True}
            self.bot.state.save()
            return (f"📲 OTP sent to <b>{mobile}</b>. Reply with:\n<code>/gtsbuy otp &lt;code&gt;</code>"
                    + (f" {label}" if label != mobile else ""))

        if sub == "otp":
            if len(args) < 2:
                return "Usage: <code>/gtsbuy otp &lt;code&gt; [label]</code>"
            otp = gc.clean_otp(args[1])
            label = args[2] if len(args) > 2 else next((l for l, a in accts.items() if a.get("pending_otp")), None)
            if not label or label not in accts:
                return "No account is waiting for an OTP. Start with <code>/gtsbuy add &lt;mobile&gt;</code>."
            try:
                await gc.verify_otp(client, self.bot.state, label, gc.login_id(accts[label]), otp)
            except gc.GtsError as e:
                return f"❌ {html.escape(str(e))}"
            return f"✅ Logged in: <b>{html.escape(label)}</b>. Try <code>/gtsbuy test &lt;card link&gt;</code>."

        if sub in ("remove", "delete", "logout"):
            label = args[1] if len(args) > 1 else None
            if label in accts:
                accts.pop(label)
                self.bot.state.save()
                return f"Removed <b>{html.escape(label)}</b>."
            return "Usage: <code>/gtsbuy remove &lt;label&gt;</code>\n\n" + acc_list()

        if sub in ("on", "off"):
            self.bot.state.settings["gts_auto"] = sub == "on"
            self.bot.state.save()
            n = sum(1 for a in accts.values() if a.get("access"))
            if sub == "on":
                return (f"🟢 <b>Auto-buy ON.</b> When GamesTheShop cards restock, each of your {n} logged-in "
                        f"account{'s' if n != 1 else ''} places one order per card and the QRs come here "
                        f"(deleted after {gc.QR_MINUTES} min).\n"
                        "Tip: switch GamesTheShop off in the laptop extension, or you'll get orders from both.")
            return "⏸ <b>Auto-buy OFF.</b> Restocks only send the usual alerts."

        if sub == "test":
            links = [a for a in args[1:] if "gamestheshop.com" in a]
            items = []
            for url in links:
                m = _re.search(r"/product/([0-9a-f-]{36})", url)
                v = _re.search(r"variant=([0-9a-f-]{36})", url)
                if m and v:
                    items.append(Item(store="gamestheshop", item_id=v.group(1), title=f"test card {len(items) + 1}",
                                      url=url, in_stock=True, extra={"product_id": m.group(1)}))
            if not items:
                return ("Usage: <code>/gtsbuy test &lt;card link&gt; [more links…]</code>\n"
                        "Links must look like <code>.../product/&lt;id&gt;?variant=&lt;id&gt;</code>.")
            logged = [l for l, a in accts.items() if a.get("access")]
            if not logged:
                return "No logged-in account. Add one with <code>/gtsbuy add &lt;mobile&gt;</code> first."

            async def run_account(label):
                """Like a real restock: every card at once; each QR is sent the moment it's ready."""
                out = []

                async def done(it, res, err, secs):
                    if err is not None:
                        out.append(f"❌ {html.escape(label)} · {it.title}: {html.escape(str(err))}")
                        return
                    try:
                        await gc.pay_to_dm(self.bot, client, label, it.title, res, [str(cid)], secs=secs)
                        out.append(f"✅ {html.escape(label)} · {it.title}: ₹{res.get('amount')} – QR sent ({secs:.1f}s)")
                    except gc.GtsError as e:
                        out.append(f"⚠️ {html.escape(label)} · {it.title}: order placed, but no QR: {html.escape(str(e))}")

                await gc.order_cards(client, self.bot.state, accts[label], items, done)
                return out

            results = await asyncio.gather(*(run_account(l) for l in logged))   # all accounts at once
            lines = [line for r in results for line in r]
            return (f"<b>Test: {len(logged)} account{'s' if len(logged) != 1 else ''} × {len(items)} card"
                    f"{'s' if len(items) != 1 else ''}</b>\n" + "\n".join(lines) +
                    "\n\n<b>These are real orders:</b> only scan a QR if you want to buy. Unpaid, they expire by themselves.")

        return ("🛒 <b>GamesTheShop auto-buy</b> (owner)\n"
                f"Status: <b>{'🟢 ON' if gc.auto_on(self.bot.state) else '⏸ OFF'}</b>\n\n"
                f"{acc_list()}\n\n"
                "<code>/gtsbuy add &lt;mobile&gt;</code> – log in an account (OTP)\n"
                "<code>/gtsbuy otp &lt;code&gt;</code> – finish the login\n"
                "<code>/gtsbuy on</code> / <code>off</code> – auto-buy on restock (one order per card per account)\n"
                "<code>/gtsbuy test &lt;link&gt; [link…]</code> – place real orders now and get their QRs\n"
                "<code>/gtsbuy remove &lt;label&gt;</code> – remove an account")

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

    async def _group_command(self, client, msg, chat) -> None:
        """Read-only commands inside a connected group, if the owner allowed them."""
        cid = str(chat.get("id"))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        if cid not in self.bot.groups.all:               # not a group the owner connected
            return
        cmd = canon(text.split()[0])
        if not self.users.cmd_allowed(cmd, "groups"):
            return
        now = time.time()                                 # one command per 8 s per group
        if now - self._group_cmd_at.get(cid, 0) < 8:
            return
        self._group_cmd_at[cid] = now
        args = text.split()[1:]
        send = lambda t: self.tg.send(client, t, chat_ids=[cid])  # noqa: E731
        if cmd == "status":
            await send(self.status_text(cid))
        elif cmd == "history":
            days = int(args[0]) if args and args[0].isdigit() else 7
            await send(self.history_text(max(1, min(days, 90)), cid))
        elif cmd == "besttime":
            await send(self.besttime_text(cid))
        elif cmd == "help":
            await send("🤖 I post PlayStation gift card restocks here.\n\n"
                       + "\n".join(f"/{c}" for c in PUBLIC_CMDS if self.users.cmd_allowed(c, "groups"))
                       + "\n\n<i>Message me privately and tap Start to get your own alerts.</i>")

    async def _on_user_button(self, client, cq, cid, data) -> None:
        if not self.users.is_admin(cid):
            await self.tg.answer_callback(client, cq["id"], "Not available")
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
            note = f"{name} refused" if action == "no" else f"{name} removed"
            if action == "rm":
                await self.tg.send(client, "You've been removed from PlayStation card alerts.", chat_ids=[target])
        elif action == "bl" and (rec or target):
            self.users.block(target, name)
            note = f"{name} blocked 🚫"
        elif action == "ub" and rec:
            self.users.unblock(target)
            note = f"{name} unblocked ♻ (still needs approving)"
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
        await self._publish_commands(client)
        offset = self.bot.state.settings.get("tg_offset")
        while not stop.is_set():
            try:
                payload = {"timeout": 50, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
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
