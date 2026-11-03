from app.games.modules.base import GameModule
from app.games.modules.game_guesser import GameGuesserModule

# The module of each game, by slug (AC-1).
MODULES: dict[str, GameModule] = {"game-guesser": GameGuesserModule()}
