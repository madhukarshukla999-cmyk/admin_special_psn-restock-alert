"""People who receive alerts.

Admins are the chat IDs in .env (TELEGRAM_CHAT_ID). Anyone else who sends /start to the bot
becomes a pending request; an admin taps Allow/Deny. Each person has their own settings
(muted stores, sold-out alerts, daily summary).
"""
from __future__ import annotations

PREFS = {"soldout": True, "summary": True}   # defaults


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

    def recipients(self, store_id: str | None = None, pref: str | None = None,
                   admins_only: bool = False) -> list[str]:
        out = []
        for cid, rec in self.active().items():
            if admins_only and not rec.get("admin"):
                continue
            if store_id and store_id in rec.get("muted", []):
                continue
            if pref and not self.pref(cid, pref):
                continue
            out.append(cid)
        return out

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
