"""The puzzle builder's command line (SPEC-04, M1-22). Today it has one mode, `--counts`.

    uv run python scripts/build_puzzles.py --counts [--slug game-guesser]

`--counts` (AC-24) reads the candidate games from IGDB and prints how many pass each filter, in
order, so the owner can tune the thresholds (`CANDIDATE_*` in `.env`, AC-22) before building.
It writes nothing and prints no game name. It asks IGDB only for the candidate game types with at
least one rating, so "games read" is that number. It needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in
`.env` (never commit or print them) and the app's own settings (`DATABASE_URL`, `SECRET_KEY`). The
database is read for the game's earlier answers and its "never" and "rebuild" exclusions; the game
must be in it (an unknown `--slug` exits 1). It takes about as long as the title import.

Exit code 0 on success, 1 when a setting is missing or IGDB or the database cannot be read.
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.content.candidates import (
    Funnel,
    FunnelCounter,
    candidates_where,
    never_igdb_ids,
    rebuild_reserved_igdb_ids,
)
from app.content.fetch import FetchError
from app.content.igdb import DropReason, IgdbClient, IgdbError, SkipReason
from app.content.settings import ContentSettings, MissingSettingError
from app.core.clock import get_now, utc_today

DEFAULT_SLUG = "game-guesser"


class UnknownGameError(Exception):
    """The database has no game with the slug that was asked for."""


@dataclass
class CountsReport:
    funnel: Funnel
    skipped: Counter[SkipReason]
    dropped: Counter[DropReason]

    def lines(self) -> list[str]:
        """The records IGDB sent that the client skipped come first (AC-8), then the funnel, which
        starts from the games that were kept. No game name, no id."""
        funnel = self.funnel
        lines = [
            f"records skipped as invalid (AC-8): {sum(self.skipped.values())}"
            + _breakdown(self.skipped)
        ]
        lines += [f"{step.name}: passed {step.passed}" for step in funnel.steps]
        lines.append(f"candidates that survive one rebuild: {funnel.survive_one_rebuild}")
        lines.append(
            f"items dropped from records that were kept: {sum(self.dropped.values())}"
            + _breakdown(self.dropped)
        )
        return lines


def _breakdown(counter: Counter[Any]) -> str:
    parts = [f"{reason}: {count}" for reason, count in sorted(counter.items()) if count]
    return f" ({', '.join(parts)})" if parts else ""


async def run_counts(settings: ContentSettings, slug: str) -> CountsReport:
    from app.core.db import async_session_maker, engine
    from app.crud.exclusion import list_exclusions
    from app.crud.game import get_game_by_slug
    from app.crud.puzzle import list_answer_igdb_ids

    try:
        now = get_now()
        async with async_session_maker() as db:
            game = await get_game_by_slug(db, slug)
            if game is None:
                raise UnknownGameError(f"no game with the slug {slug!r} in the database")
            earlier = await list_answer_igdb_ids(db, game.id)
            exclusions = await list_exclusions(db, game.id)
        counter = FunnelCounter(
            today=utc_today(now),
            settings=settings,
            earlier_answer_igdb_ids=earlier,
            never_igdb_ids=never_igdb_ids(exclusions),
            reserved_igdb_ids=rebuild_reserved_igdb_ids(exclusions),
            keep_candidates=False,
        )
        async with IgdbClient.from_settings(settings) as igdb:
            # One game at a time: only the games that pass are kept.
            async for igdb_game in igdb.games(now=now, where=candidates_where(settings)):
                counter.add(igdb_game)
            return CountsReport(counter.result(), igdb.skipped, igdb.dropped)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--counts", action="store_true", help="print how many games pass each filter"
    )
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="the game whose answers are excluded")
    args = parser.parse_args()
    if not args.counts:
        parser.error("nothing to do: use --counts")
    # httpx logs every request URL at INFO; keep the run quiet.
    logging.basicConfig(level=logging.WARNING)
    settings = ContentSettings()
    try:
        settings.require("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET")
        report = asyncio.run(run_counts(settings, args.slug))
    except (MissingSettingError, UnknownGameError) as error:
        print(f"build_puzzles: refusing to run: {error}", file=sys.stderr)
        return 1
    except (IgdbError, FetchError, httpx.HTTPError) as error:
        print(f"build_puzzles: {error}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError) as error:
        print(f"build_puzzles: database error: {type(error).__name__}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
