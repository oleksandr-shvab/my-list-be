"""Fill the title list from IGDB (M1-21, SPEC-04 AC-10 to AC-19). Safe to run again.

    ENVIRONMENT=local uv run python scripts/import_titles.py --target local

Needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in `.env` (never commit or print them) and the app's
own settings (`DATABASE_URL`, `SECRET_KEY`). It reads the games of AC-11 from IGDB, then writes
them to the database `DATABASE_URL` names. `--target` is required and must equal the ENVIRONMENT
variable; a target other than `local` also needs `--confirm-target <name>` (or the name typed at the
prompt), and nothing runs in CI. The run prints the target before it writes.

It adds and updates titles, and never deletes one. It commits every 1,000 titles, so a stopped run
keeps what it wrote and a run again carries on. At the end it prints the counts of AC-19.

Exit code 0 on success, 1 when the guard refuses, a setting is missing or IGDB cannot be read.
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
from app.content.igdb import IgdbClient, IgdbError
from app.content.settings import ContentSettings, MissingSettingError
from app.content.target import TargetRefusedError, check_target
from app.content.titles_import import IGDB_WHERE, ImportReport, select_records, upsert_titles
from app.core.clock import get_now


async def run(settings: ContentSettings) -> ImportReport:
    from app.core.db import async_session_maker, engine

    try:
        async with IgdbClient.from_settings(settings) as igdb:
            selection = await select_records(igdb.games(now=get_now(), where=IGDB_WHERE))
            skipped, dropped = igdb.skipped, igdb.dropped
        print(f"Read {len(selection.records)} titles from IGDB; writing.")
        async with async_session_maker() as db:
            counts = await upsert_titles(db, selection.records)
        return ImportReport(selection, counts, skipped=skipped, dropped=dropped)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", required=True, help="where to write; must equal ENVIRONMENT")
    parser.add_argument("--confirm-target", help="the target name again, for a non-local target")
    args = parser.parse_args()
    # httpx logs every request URL at INFO; keep the run quiet.
    logging.basicConfig(level=logging.WARNING)
    settings = ContentSettings()
    try:
        settings.require("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET")
        target = check_target(
            args.target,
            args.confirm_target,
            ask=input if sys.stdin.isatty() else None,
        )
        print(target.describe())
        report = asyncio.run(run(settings))
    except (TargetRefusedError, MissingSettingError) as error:
        print(f"import_titles: refusing to run: {error}", file=sys.stderr)
        return 1
    except (IgdbError, FetchError, httpx.HTTPError) as error:
        print(f"import_titles: {error}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
