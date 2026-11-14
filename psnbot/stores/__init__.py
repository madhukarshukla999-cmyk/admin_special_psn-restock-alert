from .amazon import Amazon
from .blinkit import Blinkit
from .digistore import DigiStore
from .flipkart import Flipkart
from .gamestheshop import GamesTheShop
from .gyftr import Gyftr
from .hubble import Hubble
from .jiogames import JioGames
from .rooter import Rooter

from .shop import PLATFORMS, Shop

REGISTRY = {cls.id: cls for cls in (Gyftr, GamesTheShop, DigiStore, Flipkart, Amazon, JioGames, Blinkit, Rooter, Hubble)}


def make_store(sid: str, scfg: dict, title_filter):
    """A built-in store, or a generic Shopify/WooCommerce shop (config has `platform:`). None = unknown."""
    if sid in REGISTRY:
        return REGISTRY[sid](scfg, title_filter)
    if str(scfg.get("platform", "")).lower() in PLATFORMS:
        return Shop(sid, scfg, title_filter)
    return None
