"""Restock history and 'best time' guesses.

Every restock opens an event; the matching sell-out closes it, so we know when cards come
back and how long they last. Stored in state.json (last 90 days).
"""
from __future__ import annotations

import time
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
KEEP_DAYS = 90
MAX_EVENTS = 5000


def _ist(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} days"


def record_restock(events: list[dict], key: str, store: str, title: str, price, now: float | None = None) -> None:
    now = now or time.time()
    events.append({"key": key, "store": store, "title": title, "price": price, "start": now, "end": None})
    prune(events, now)


def record_soldout(events: list[dict], key: str, now: float | None = None) -> float | None:
    """Close the open event for `key`. Returns how long it was in stock (seconds)."""
    now = now or time.time()
    for ev in reversed(events):
        if ev["key"] == key:
            if ev.get("end") is None:
                ev["end"] = now
                return now - ev["start"]
            return None
    return None


def open_since(events: list[dict], key: str) -> float | None:
    for ev in reversed(events):
        if ev["key"] == key:
            return ev["start"] if ev.get("end") is None else None
    return None


def prune(events: list[dict], now: float | None = None) -> None:
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    events[:] = [e for e in events if e["start"] >= cutoff][-MAX_EVENTS:]


def recent(events: list[dict], seconds: float, now: float | None = None) -> list[dict]:
    cutoff = (now or time.time()) - seconds
    return [e for e in events if e["start"] >= cutoff]


def _mod(t: datetime) -> int:
    return t.hour * 60 + t.minute


def _circ(a: int, b: int) -> int:
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def best_window(evs: list[dict], min_events: int = 3, min_days: int = 2, spread: int = 60) -> dict | None:
    """Time of day (IST) when most restocks happen: the busiest ±1 h cluster, trimmed to
    the restocks actually in it. None if there's too little data."""
    if len(evs) < min_events:
        return None
    days = {_ist(e["start"]).date() for e in evs}
    if len(days) < min_days:
        return None
    mins = [_mod(_ist(e["start"])) for e in evs]
    centre = max(sorted(set(mins)), key=lambda c: sum(1 for m in mins if _circ(m, c) <= spread))
    cluster = [(m, e) for m, e in zip(mins, evs) if _circ(m, centre) <= spread]
    offs = [((m - centre + 720) % 1440) - 720 for m, _ in cluster]     # minutes relative to centre
    lo = (centre + min(offs)) // 15 * 15
    hi = -(-(centre + max(offs)) // 15) * 15
    if hi - lo < 30:            # show at least a 30-minute window
        hi = lo + 30
    durs = [e["end"] - e["start"] for _, e in cluster if e.get("end")]
    return {"from": lo % 1440, "to": hi % 1440, "count": len(cluster), "total": len(evs),
            "days": len({_ist(e["start"]).date() for _, e in cluster}), "all_days": len(days),
            "typical_duration": median(durs) if durs else None}


def fmt_window(w: dict) -> str:
    f, t = w["from"], w["to"]
    span = f"{f // 60:02d}:{f % 60:02d}–{t // 60:02d}:{t % 60:02d} IST"
    s = f"usually <b>{span}</b> ({w['count']} of {w['total']} restocks, on {w['days']} days)"
    if w.get("typical_duration") is not None:
        s += f", lasts ~{fmt_duration(w['typical_duration'])}"
    return s


def best_times(events: list[dict], store_names: dict[str, str], days: int = 30) -> list[str]:
    evs = recent(events, days * 86400)
    lines = []
    by_store: dict[str, list[dict]] = {}
    by_card: dict[str, list[dict]] = {}
    for e in evs:
        by_store.setdefault(e["store"], []).append(e)
        by_card.setdefault(e["key"], []).append(e)
    for sid, se in sorted(by_store.items(), key=lambda kv: -len(kv[1])):
        w = best_window(se)
        name = store_names.get(sid, sid)
        lines.append(f"<b>{name}</b>: {fmt_window(w)}" if w else
                     f"<b>{name}</b>: not enough data yet ({len(se)} restock{'s' if len(se) != 1 else ''})")
        for key, ce in by_card.items():
            if ce[0]["store"] != sid or len(by_card) == 1:
                continue
            cw = best_window(ce)
            if cw:
                lines.append(f"   • {ce[-1]['title'][:60]}: {fmt_window(cw)}")
    return lines
