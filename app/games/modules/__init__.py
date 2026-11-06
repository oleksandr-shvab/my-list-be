from app.games.modules.base import GameModule
from app.games.modules.game_guesser import GameGuesserModule

MODULES: dict[str, GameModule] = {"game-guesser": GameGuesserModule()}
