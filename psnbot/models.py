from __future__ import annotations

import re
from dataclasses import dataclass, field

# Matches "Rs.1000", "Rs 1,000", "₹2000", "INR 3000", "1000 INR", "5000-inr"
_DENOM_RE = re.compile(
    r"(?:rs\.?|₹|inr)\s*-?\s*([\d,]{3,6})(?![\d])|(?<![\d])([\d,]{3,6})\s*-?\s*(?:inr|rs)\b",
    re.IGNORECASE,
)


def parse_denomination(text: str) -> int | None:
    m = _DENOM_RE.search(text or "")
    if not m:
        return None
    try:
        return int((m.group(1) or m.group(2)).replace(",", ""))
    except ValueError:
        return None


@dataclass
class Item:
    store: str            # store id, e.g. "flipkart"
    item_id: str          # stable id within the store
    title: str
    url: str
    # True = buyable, False = out of stock, None = could not determine this cycle
    in_stock: bool | None
    price: float | None = None   # what you pay
    mrp: float | None = None     # face value / list price
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.store}:{self.item_id}"

    @property
    def denomination(self) -> int | None:
        return parse_denomination(self.title) or (int(self.mrp) if self.mrp else None)


class StoreError(Exception):
    """Raised when a store could not be checked at all (blocked, layout change, etc.)."""


class TitleFilter:
    """Decides whether a product title is a PlayStation gift card."""

    def __init__(self, cfg: dict):
        self.include = [w.lower() for w in cfg.get("include_any", ["playstation", "psn"])]
        self.card_words = [w.lower() for w in cfg.get(
            "card_words_any", ["gift card", "giftcard", "wallet", "voucher", "top up", "topup", "store card"])]
        self.exclude = [w.lower() for w in cfg.get("exclude_any", [])]

    def matches(self, title: str, require_card_word: bool = True) -> bool:
        t = " ".join((title or "").lower().replace("-", " ").split())
        if not any(w in t for w in self.include):
            return False
        if any(w in t for w in self.exclude):
            return False
        if require_card_word and not any(w in t for w in self.card_words):
            return False
        return True
