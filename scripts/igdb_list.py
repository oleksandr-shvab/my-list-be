"""Print a few games from IGDB with every field the content scripts use (AC-9). Read-only: it
writes nothing, needs no target and handles no puzzle answers.

    uv run python scripts/igdb_list.py --limit 5

Needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in the environment or in `.env` (never commit or print
them). A field IGDB does not have for a game is printed as "missing". By default it asks for games
with at least 100 ratings, so the fields are likely to be there; `--where` takes any Apicalypse
condition instead (for example `--where "game_type = 0"`).

Exit code 0 on success, 1 when a setting is missing, the condition is refused or IGDB cannot be
read (the message says why).
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.content.fetch import FetchError
from app.content.igdb import GameType, IgdbClient, IgdbError, IgdbGame
from app.content.settings import ContentSettings, MissingSettingError
from app.core.clock import get_now

MISSING = "missing"
DEFAULT_WHERE = "total_rating_count >= 100"
# Keeps a script run quick; the client itself pages up to 500.
MAX_LIMIT = 50


def plain(text: str) -> str:
    """IGDB text is data: show it with no control or escape characters, so it cannot rewrite the
    terminal."""
    return "".join(char if char.isprintable() else "?" for char in text)


def joined(values: list[str]) -> str:
    return ", ".join(plain(value) for value in values) or MISSING


def shown(value: object) -> str:
    return MISSING if value is None else plain(str(value))


def describe_game_type(value: int | None) -> str:
    if value is None:
        return MISSING
    try:
        return f"{value} ({GameType(value).name.lower()})"
    except ValueError:
        return str(value)


def describe(game: IgdbGame) -> list[str]:
    franchise = game.franchise.name if game.franchise else None
    screenshots = [f"{s.image_id} {s.width}x{s.height}" for s in game.screenshots]
    return [
        f"id: {game.id}",
        f"name: {plain(game.name)}",
        f"slug: {plain(game.slug)}",
        f"release year: {shown(game.release_year)}",
        f"game type: {describe_game_type(game.game_type)}",
        f"version of (game id): {shown(game.version_parent)}",
        f"parent game (id): {shown(game.parent_game)}",
        f"main franchise: {shown(franchise)}",
        f"series: {joined([item.name for item in game.collections])}",
        f"other franchises: {joined([item.name for item in game.franchises])}",
        f"platforms of the first release: {joined(game.first_release_platforms)}",
        f"genres: {joined([item.name for item in game.genres])}",
        f"developers: {joined(game.developers)}",
        f"critic score: {shown(game.aggregated_rating)}",
        f"critic scores (count): {shown(game.aggregated_rating_count)}",
        f"ratings (count): {shown(game.total_rating_count)}",
        f"screenshots: {len(screenshots) or MISSING}",
        *(f"  {line}" for line in screenshots),
    ]


async def list_games(limit: int, where: str) -> int:
    settings = ContentSettings()
    try:
        async with IgdbClient.from_settings(settings) as igdb:
            count = 0
            async for game in igdb.games(now=get_now(), where=where, page_size=limit):
                print("\n".join(describe(game)))
                print()
                count += 1
                if count == limit:
                    break
            skipped = {reason.value: n for reason, n in igdb.skipped.items()}
            dropped = {reason.value: n for reason, n in igdb.dropped.items()}
    except (MissingSettingError, IgdbError, FetchError, httpx.HTTPError) as error:
        print(f"igdb_list: {error}", file=sys.stderr)
        return 1
    print(f"{count} game(s) listed")
    if skipped:
        print(f"records skipped: {skipped}")
    if dropped:
        print(f"items dropped from kept records: {dropped}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=5, help=f"1 to {MAX_LIMIT} (default 5)")
    parser.add_argument("--where", default=DEFAULT_WHERE, help="an Apicalypse condition")
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error(f"--limit must be 1 to {MAX_LIMIT}")
    # httpx logs every request URL at INFO; keep the run quiet.
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(list_games(args.limit, args.where))


if __name__ == "__main__":
    sys.exit(main())
