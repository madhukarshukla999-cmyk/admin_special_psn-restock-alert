"""Weekly report and the 'while you were in quiet hours' message."""
from __future__ import annotations

import html
import time
from collections import Counter
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from . import history as hist
from .models import parse_denomination
from .users import hm

IST = ZoneInfo("Asia/Kolkata")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _ist(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def _rs(v) -> str:
    try:
        return f"₹{float(v):,.0f}"
    except (TypeError, ValueError):
        return "₹?"


def _values(evs: list[dict]) -> str:
    vals = sorted({v for v in (parse_denomination(e.get("title", "")) for e in evs) if v})
    if not vals:
        return ""
    if len(vals) <= 4:
        return ", ".join(f"₹{v:,}" for v in vals)
    return f"₹{vals[0]:,}–₹{vals[-1]:,}"


def weekly_text(bot, cid: str, now: float | None = None) -> str:
    now = now or time.time()
    muted = bot.users.muted(cid)
    week = [e for e in hist.recent(bot.state.history, 7 * 86400, now) if e["store"] not in muted]
    prev = [e for e in bot.state.history if now - 14 * 86400 <= e["start"] < now - 7 * 86400
            and e["store"] not in muted]
    start, end = _ist(now - 6 * 86400), _ist(now)
    lines = [f"📊 <b>Your weekly report</b> · {start.strftime('%d %b')} – {end.strftime('%d %b')}", ""]

    if not week:
        lines.append("😴 No PlayStation card restocks this week in the stores you follow.")
    else:
        trend = ""
        if prev:
            diff = len(week) - len(prev)
            trend = f" ({'▲' if diff > 0 else '▼' if diff < 0 else '='} {abs(diff)} vs last week)" if diff else " (same as last week)"
        lines.append(f"🟢 <b>{len(week)} restock{'s' if len(week) != 1 else ''}</b>{trend}")
        lines.append("")

        by_store: dict[str, list[dict]] = {}
        for e in week:
            by_store.setdefault(e["store"], []).append(e)
        quick = []
        for sid, evs in sorted(by_store.items(), key=lambda kv: -len(kv[1])):
            closed = [e["end"] - e["start"] for e in evs if e.get("end")]
            parts = [f"{len(evs)}×"]
            if closed:
                med = median(closed)
                parts.append(f"usually lasted <b>{hist.fmt_duration(med)}</b>")
                parts.append(f"longest {hist.fmt_duration(max(closed))}")
                quick.append((med, sid))
            open_now = sum(1 for e in evs if not e.get("end"))
            if open_now:
                parts.append(f"{open_now} still in stock")
            v = _values(evs)
            lines.append(f"🏪 <b>{html.escape(bot.names.get(sid, sid))}</b> · " + " · ".join(parts))
            if v:
                lines.append(f"      💳 {v}")

        days = Counter(_ist(e["start"]).weekday() for e in week)
        hours = Counter(_ist(e["start"]).hour for e in week)
        top_days = [DAYS[d] for d, n in days.most_common() if n == days.most_common(1)[0][1]][:3]
        h, _ = hours.most_common(1)[0]
        lines += ["", f"⏰ Most restocks: <b>{' & '.join(top_days)}</b>, around <b>{h:02d}:00–{(h + 1) % 24:02d}:00</b>"]
        if quick:
            med, sid = min(quick)
            lines.append(f"⚡ Sells out fastest: <b>{html.escape(bot.names.get(sid, sid))}</b> "
                         f"(about {hist.fmt_duration(med)}) – be quick there")

    if bot.users.is_admin(cid):
        buys = [p for p in bot.state.settings.get("purchases", []) if p.get("ts", 0) >= now - 7 * 86400]
        lines.append("")
        if buys:
            total = sum(float(p.get("amount") or 0) for p in buys)
            lines.append(f"🛒 <b>You bought {len(buys)} card{'s' if len(buys) != 1 else ''}</b> · total <b>{_rs(total)}</b>")
            for p in sorted(buys, key=lambda p: p["ts"])[:15]:
                who = " · ".join(x for x in (p.get("account"), p.get("profile")) if x)
                lines.append(f"   • {_ist(p['ts']).strftime('%a %d %b %H:%M')} · {html.escape(str(p.get('store') or '?'))} "
                             f"{_rs(p.get('amount'))}" + (f" · {html.escape(who)}" if who else ""))
        else:
            lines.append("🛒 No purchases recorded this week "
                         "<i>(counted when the browser extension sees a successful payment)</i>.")

    lines += ["", "<i>/history for every restock · /besttime for usual times · /menu to change what you get</i>"]
    return "\n".join(lines)


def digest_text(bot, cid: str, missed: list[dict]) -> str:
    q = bot.users.quiet(cid) or (bot.users.get(cid) or {}).get("quiet") or {}
    span = f" ({hm(q['start'])}–{hm(q['end'])})" if q.get("start") is not None else ""
    lines = [f"🌅 <b>While you were in quiet hours{span}</b>", ""]
    seen = set()
    rows = []
    for m in sorted(missed, key=lambda m: m["ts"]):
        k = (m["key"], round(m["ts"] / 60))
        if k in seen:
            continue
        seen.add(k)
        ev = next((e for e in reversed(bot.state.history)
                   if e["key"] == m["key"] and abs(e["start"] - m["ts"]) < 300), None)
        still = bool(bot.state.items.get(m["key"], {}).get("in_stock"))
        if ev and ev.get("end"):
            state = f"sold out after {hist.fmt_duration(ev['end'] - ev['start'])}"
        elif still:
            state = f'🟢 <b>still in stock</b> – <a href="{html.escape(m["url"], quote=True)}">buy now</a>'
        else:
            state = "sold out"
        price = f" · {_rs(m['price'])}" if m.get("price") and not parse_denomination(m.get("title", "")) else ""
        rows.append(f"• {_ist(m['ts']).strftime('%H:%M')} · <b>{html.escape(bot.names.get(m['store'], m['store']))}</b> · "
                    f"{html.escape(m['title'][:50])}{price} · {state}")
    lines.append(f"{len(rows)} restock{'s' if len(rows) != 1 else ''} came and went:" if rows else "Nothing happened.")
    lines += rows[:25]
    if len(rows) > 25:
        lines.append(f"…and {len(rows) - 25} more (/history)")
    lines += ["", "<i>/status shows what's in stock right now.</i>"]
    return "\n".join(lines)
