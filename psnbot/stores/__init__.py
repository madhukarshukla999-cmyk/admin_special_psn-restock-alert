from .amazon import Amazon
from .digistore import DigiStore
from .flipkart import Flipkart
from .gamestheshop import GamesTheShop
from .gyftr import Gyftr
from .jiogames import JioGames

REGISTRY = {cls.id: cls for cls in (Gyftr, GamesTheShop, DigiStore, Flipkart, Amazon, JioGames)}
