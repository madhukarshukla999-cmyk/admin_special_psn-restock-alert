"""People who receive alerts.

Admins are the chat IDs in .env (TELEGRAM_CHAT_ID). Anyone else who sends /start to the bot
becomes a pending request; an admin taps Allow/Deny. Each person has their own settings
(muted stores, sold-out alerts, daily summary).
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PREFS = {"soldout": True, "summary": True, "weekly": True}   # defaults
CARD_VALUES = [500, 1000, 1500, 2000, 2500, 3000, 4000, 5000]


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hm(raw: str) -> int | None:
    """'23:30', '23.30', '2330', '23', '11pm', '7am' -> minutes after midnight."""
    s = raw.strip().lower().replace(".", ":")
    pm, am = s.endswith("pm"), s.endswith("am")
    s = s[:-2].strip() if (pm or am) else s
    if ":" in s:
        h, _, m = s.partition(":")
    elif len(s) in (3, 4) and s.isdigit():
        h, m = s[:-2], s[-2:]
    else:
        h, m = s, "0"
    if not (h.isdigit() and m.isdigit()):
        return None
    h, m = int(h), int(m)
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def rule_text(rule: dict | None) -> str:
    rule = rule or {}
    if rule.get("only"):
        return "only " + ", ".join(f"₹{v:,}" for v in sorted(rule["only"]))
    if rule.get("min"):
        return f"₹{rule['min']:,} and above"
    return "all values"


class Users:
    def __init__(self, state, admin_ids: list[str]):
        self.state = state
        self.admins = {str(a) for a in admin_ids}
        users = state.settings.setdefault("users", {})
        legacy_muted = state.settings.pop("muted", None)   # from the single-user version
        for a in self.admins:
            rec = users.setdefault(a, {"name": "Owner"})
            rec["admin"] = True
            rec.pop("pending", None)
            if legacy_muted is not None and "muted" not in rec:
                rec["muted"] = list(legacy_muted)
        for cid, rec in users.items():
            if cid not in self.admins:
                rec["admin"] = False

    @property
    def all(self) -> dict[str, dict]:
        return self.state.settings["users"]

    def get(self, cid) -> dict | None:
        return self.all.get(str(cid))

    def is_admin(self, cid) -> bool:
        return str(cid) in self.admins

    def is_allowed(self, cid) -> bool:
        rec = self.get(cid)
        return bool(rec and (rec.get("admin") or rec.get("approved")))

    def active(self) -> dict[str, dict]:
        return {c: r for c, r in self.all.items() if r.get("admin") or r.get("approved")}

    def pending(self) -> dict[str, dict]:
        return {c: r for c, r in self.all.items() if r.get("pending")}

    def muted(self, cid) -> set[str]:
        return set((self.get(cid) or {}).get("muted", []))

    def set_muted(self, cid, muted: set[str]) -> None:
        self.all[str(cid)]["muted"] = sorted(muted)

    def pref(self, cid, name: str) -> bool:
        return bool((self.get(cid) or {}).get(name, PREFS[name]))

    def toggle_pref(self, cid, name: str) -> bool:
        rec = self.all[str(cid)]
        rec[name] = not self.pref(cid, name)
        return rec[name]

    # -- owner-only: private stores + alert delays (never shown to anyone else) ----
    def private(self) -> set[str]:
        return set(self.state.settings.get("private_stores", []))

    def hidden(self, cid) -> set[str]:
        """Stores this person must not see at all (private ones, unless they're the owner)."""
        return set() if self.is_admin(cid) else self.private()

    def delay(self, cid) -> int:
        """Seconds this person's restock alerts wait (owner: always 0)."""
        if self.is_admin(cid):
            return 0
        own = (self.get(cid) or {}).get("delay")
        return int(own if own is not None else (self.state.settings.get("delays") or {}).get("users", 0))

    def recipients(self, store_id: str | None = None, pref: str | None = None,
                   admins_only: bool = False) -> list[str]:
        out = []
        private = self.private()
        for cid, rec in self.active().items():
            if admins_only and not rec.get("admin"):
                continue
            if store_id and store_id in rec.get("muted", []):
                continue
            if store_id and store_id in private and not rec.get("admin"):
                continue
            if pref and not self.pref(cid, pref):
                continue
            out.append(cid)
        return out

    # -- quiet hours -----------------------------------------------------
    def quiet(self, cid) -> dict | None:
        """{'start': min, 'end': min, 'allow': [store ids that still alert]} (IST), or None."""
        q = (self.get(cid) or {}).get("quiet")
        return q if q and q.get("on", True) else None

    def set_quiet(self, cid, start: int | None, end: int | None = None) -> None:
        rec = self.all[str(cid)]
        old = rec.get("quiet") or {}
        if start is None:
            if old:
                old["on"] = False          # keep times + exceptions for next time
            return
        rec["quiet"] = {"start": start, "end": end, "allow": old.get("allow", []), "on": True}

    def toggle_quiet_allow(self, cid, store_id: str) -> bool:
        q = self.all[str(cid)].setdefault("quiet", {"start": 23 * 60, "end": 7 * 60, "allow": [], "on": False})
        allow = set(q.get("allow", []))
        allow ^= {store_id}
        q["allow"] = sorted(allow)
        return store_id in allow

    def in_quiet(self, cid, store_id: str | None = None, now: float | None = None) -> bool:
        q = self.quiet(cid)
        if not q:
            return False
        if store_id and store_id in q.get("allow", []):
            return False
        d = datetime.fromtimestamp(now or time.time(), IST)
        m, s, e = d.hour * 60 + d.minute, q["start"], q["end"]
        return s <= m < e if s < e else (m >= s or m < e)

    def add_missed(self, cid, items) -> None:
        rec = self.all[str(cid)]
        lst = rec.setdefault("missed", [])
        now = time.time()
        for it in items:
            lst.append({"key": it.key, "store": it.store, "title": it.title, "url": it.url,
                        "price": it.price, "ts": now})
        rec["missed"] = lst[-60:]

    # -- card values -----------------------------------------------------
    def value_rule(self, cid, store_id: str | None = None) -> dict:
        vals = (self.get(cid) or {}).get("values") or {}
        if store_id and store_id in vals:
            return vals[store_id]
        return vals.get("*") or {}

    def has_store_rule(self, cid, store_id: str) -> bool:
        return store_id in ((self.get(cid) or {}).get("values") or {})

    def set_value_rule(self, cid, store_id: str, rule: dict | None) -> None:
        vals = self.all[str(cid)].setdefault("values", {})
        if rule is None:
            vals.pop(store_id, None)
        else:
            vals[store_id] = rule

    def wants_value(self, cid, store_id: str, denom) -> bool:
        if not denom:
            return True                       # unknown value: better to tell them
        rule = self.value_rule(cid, store_id)
        if rule.get("only"):
            return int(denom) in rule["only"]
        return int(denom) >= int(rule.get("min") or 0)

    # -- WhatsApp / email ------------------------------------------------
    def channel(self, cid, name: str) -> dict | None:
        """{'phone','apikey','on'} for whatsapp, {'address','on'} for email; None if not set up."""
        return (self.get(cid) or {}).get(name)

    def set_channel(self, cid, name: str, data: dict | None) -> None:
        rec = self.all[str(cid)]
        if data is None:
            rec.pop(name, None)
        else:
            rec[name] = data

    def channel_on(self, cid, name: str) -> bool:
        ch = self.channel(cid, name)
        return bool(ch and ch.get("on", True))

    def channel_recipients(self, name: str, store_id: str | None = None, pref: str | None = None) -> list[tuple[str, dict]]:
        return [(cid, self.channel(cid, name)) for cid in self.recipients(store_id=store_id, pref=pref)
                if self.channel_on(cid, name)]

    # -- requests --------------------------------------------------------
    def request(self, cid, name: str) -> bool:
        """Register a join request. Returns True if it's new (admins should be told)."""
        cid = str(cid)
        if cid in self.all:
            return False
        self.all[cid] = {"name": name, "pending": True}
        return True

    def approve(self, cid) -> None:
        rec = self.all[str(cid)]
        rec.pop("pending", None)
        rec["approved"] = True

    def remove(self, cid) -> None:
        if str(cid) not in self.admins:
            self.all.pop(str(cid), None)
