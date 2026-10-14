from .amazon import Amazon
from .blinkit import Blinkit
from .digistore import DigiStore
from .flipkart import Flipkart
from .gamestheshop import GamesTheShop
from .gyftr import Gyftr
from .jiogames import JioGames
from .rooter import Rooter

REGISTRY = {cls.id: cls for cls in (Gyftr, GamesTheShop, DigiStore, Flipkart, Amazon, JioGames, Blinkit, Rooter)}
