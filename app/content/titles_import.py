"""Fill the title list from IGDB (SPEC-04 AC-10 to AC-19).

Two steps. `select_records` reads the games from IGDB (any async iterable of `IgdbGame`) and keeps
what the list needs, one record per title (AC-14); it touches no database. `upsert_titles` writes
the records: it matches stored titles by IGDB id, never deletes anything (AC-17), and leaves a title
unchanged, with a count, when the change could break a stored game (AC-18, R-15). It commits every
`BATCH_SIZE` titles, so a stopped run keeps its progress and a run again continues (AC-16).
"""

import re
from collections import Counter
from collections.abc import AsyncIterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.content.franchise_overrides import FRANCHISE_OVERRIDES, FranchiseSource
from app.content.igdb import DropReason, GameType, IgdbGame, IgdbNamed, SkipReason
from app.crud.title import (
    create_title,
    find_near_duplicate,
    get_titles_by_igdb_ids,
    referenced_title_ids,
    title_changes,
    update_title,
)
from app.games.normalize import normalize_text
from app.models.title import Title, TitleKind

BATCH_SIZE = 1000
# The column sizes of `titles` (`app/models/title.py`). NFKD can make a normalized name longer.
MAX_NAME_LENGTH = 255
MAX_FRANCHISE_LENGTH = 255
MAX_NORMALIZED_NAME_LENGTH = 1024

# AC-11: main games, remakes, remasters, expanded games and standalone expansions.
IN_SCOPE_TYPES = frozenset(
    {
        GameType.MAIN_GAME,
        GameType.REMAKE,
        GameType.REMASTER,
        GameType.EXPANDED_GAME,
        GameType.STANDALONE_EXPANSION,
    }
)
# What the script asks IGDB for, so a full run does not read the whole catalogue. `in_scope` is the
# rule; this only narrows the request to the same set.
IGDB_WHERE = (
    f"game_type = ({','.join(str(int(kind)) for kind in sorted(IN_SCOPE_TYPES))})"
    " & version_parent = null & total_rating_count >= 1"
)
# A trailing qualifier in parentheses, such as "Zelda (series)".
TRAILING_QUALIFIER = re.compile(r"\s*\([^()]*\)\s*$")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ImportRecord:
    """What the list keeps of one IGDB game."""

    igdb_id: int
    name: str
    normalized_name: str
    franchise: str | None
    year: int | None
    ratings: int

    @property
    def key(self) -> tuple[str, int | None]:
        """Two records with one key would be one title in the list (AC-14)."""
        return (self.normalized_name, self.year)

    @property
    def rank(self) -> tuple[int, int]:
        """The better record has the higher rank: more ratings, then the lower IGDB id."""
        return (self.ratings, -self.igdb_id)


def in_scope(game: IgdbGame) -> bool:
    """AC-10, AC-11. Candidate answers are a subset of this list by construction (R-20)."""
    return (
        game.game_type in IN_SCOPE_TYPES
        and game.version_parent is None
        and (game.total_rating_count or 0) >= 1
    )


def clean_franchise(name: str) -> str:
    """Trim, collapse spaces and drop a trailing qualifier such as "(series)" (AC-12)."""
    cleaned = WHITESPACE.sub(" ", name).strip()
    return TRAILING_QUALIFIER.sub("", cleaned).strip()


def franchise_of(game: IgdbGame) -> str | None:
    """IGDB's main franchise, else the game's first series (collection) by IGDB id (AC-12, Q4);
    then the override list. A name that is empty after cleaning, or too long for the column, is
    passed over for the next one."""
    candidates: list[tuple[FranchiseSource, IgdbNamed]] = []
    if game.franchise is not None:
        candidates.append((FranchiseSource.FRANCHISE, game.franchise))
    candidates.extend(
        (FranchiseSource.COLLECTION, item) for item in sorted(game.collections, key=lambda c: c.id)
    )
    for source, item in candidates:
        name = FRANCHISE_OVERRIDES.get((source, item.id)) or clean_franchise(item.name)
        if name and len(name) <= MAX_FRANCHISE_LENGTH and normalize_text(name):
            return name
    return None


class NameProblem(StrEnum):
    """Why a game's name cannot be a title: the `titles` table cannot hold it."""

    TOO_LONG = "too long"
    UNUSABLE = "unusable"


def to_record(game: IgdbGame) -> ImportRecord | NameProblem:
    """Map one in-scope game. A name over 255 characters (IGDB does not limit them and the client
    does not check) or one that normalizes to nothing, or to over 1024 characters, is a
    `NameProblem`: the record is skipped and counted, never cut short or stored."""
    if len(game.name) > MAX_NAME_LENGTH:
        return NameProblem.TOO_LONG
    normalized = normalize_text(game.name)
    if not normalized or len(normalized) > MAX_NORMALIZED_NAME_LENGTH:
        return NameProblem.UNUSABLE
    return ImportRecord(
        igdb_id=game.id,
        name=game.name,
        normalized_name=normalized,
        franchise=franchise_of(game),
        year=game.release_year,
        ratings=game.total_rating_count or 0,
    )


@dataclass
class Selection:
    """Step one's result. The counts are of records, not of titles."""

    records: list[ImportRecord] = field(default_factory=list)
    out_of_scope: int = 0
    near_duplicates: int = 0
    name_problems: Counter[NameProblem] = field(default_factory=Counter)


async def select_records(games: AsyncIterable[IgdbGame]) -> Selection:
    """Keep one record per (normalized name, year): the one with more ratings, then the lower IGDB
    id, whatever order IGDB sent them in (AC-14). Records come back in IGDB id order."""
    selection = Selection()
    best: dict[tuple[str, int | None], ImportRecord] = {}
    async for game in games:
        if not in_scope(game):
            selection.out_of_scope += 1
            continue
        record = to_record(game)
        if isinstance(record, NameProblem):
            selection.name_problems[record] += 1
            continue
        current = best.get(record.key)
        if current is None:
            best[record.key] = record
            continue
        selection.near_duplicates += 1
        if record.rank > current.rank:
            best[record.key] = record
    selection.records = sorted(best.values(), key=lambda record: record.igdb_id)
    return selection


@dataclass
class UpsertCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    near_duplicates: int = 0
    refused: int = 0
    # The titles of the refused count, by database id (no names: a title may be a puzzle answer).
    refused_title_ids: list[int] = field(default_factory=list)


def _batches(records: Sequence[ImportRecord], size: int) -> Iterator[Sequence[ImportRecord]]:
    for start in range(0, len(records), size):
        yield records[start : start + size]


async def upsert_titles(
    db: AsyncSession, records: Sequence[ImportRecord], *, batch_size: int = BATCH_SIZE
) -> UpsertCounts:
    """Write the records. A stored title is matched by IGDB id; one that is not in `records` is
    never touched, let alone deleted (AC-17). A new record that has the name and year of a stored
    title is a near-duplicate and is not added (AC-14, AC-17)."""
    counts = UpsertCounts()
    for batch in _batches(records, batch_size):
        stored = await get_titles_by_igdb_ids(db, [record.igdb_id for record in batch])
        changed: list[tuple[Title, ImportRecord, frozenset[str]]] = []
        for record in batch:
            title = stored.get(record.igdb_id)
            if title is None:
                await _add(db, record, counts)
                continue
            changes = title_changes(
                title, name=record.name, franchise=record.franchise, year=record.year
            )
            if changes:
                changed.append((title, record, changes))
            else:
                counts.unchanged += 1
        await _update(db, changed, counts)
        await db.commit()
    return counts


async def _add(db: AsyncSession, record: ImportRecord, counts: UpsertCounts) -> None:
    duplicate = await find_near_duplicate(
        db, kind=TitleKind.VIDEO_GAME, normalized_name=record.normalized_name, year=record.year
    )
    if duplicate is not None:
        counts.near_duplicates += 1
        return
    await create_title(
        db, name=record.name, franchise=record.franchise, year=record.year, igdb_id=record.igdb_id
    )
    counts.added += 1


# The columns a stored game's replay reads (AC-18). The year and the display texts are not read.
REPLAY_COLUMNS = frozenset({"normalized_name", "normalized_franchise"})


async def _update(
    db: AsyncSession,
    changed: Sequence[tuple[Title, ImportRecord, frozenset[str]]],
    counts: UpsertCounts,
) -> None:
    """Apply the changes, except those AC-18 refuses. `kind` never changes here: every imported
    title is a video game."""
    touching_replay = [title.id for title, _, changes in changed if changes & REPLAY_COLUMNS]
    protected = await referenced_title_ids(db, touching_replay)
    for title, record, changes in changed:
        if title.id in protected:
            counts.refused += 1
            counts.refused_title_ids.append(title.id)
            continue
        # A rename onto the name and year of another stored title would make a near-duplicate.
        if changes & {"normalized_name", "year"}:
            duplicate = await find_near_duplicate(
                db,
                kind=TitleKind.VIDEO_GAME,
                normalized_name=record.normalized_name,
                year=record.year,
                exclude_id=title.id,
            )
            if duplicate is not None:
                counts.near_duplicates += 1
                continue
        await update_title(
            db, title, name=record.name, franchise=record.franchise, year=record.year
        )
        counts.updated += 1


@dataclass
class ImportReport:
    """AC-19. `skipped` (whole records IGDB's data failed) and `dropped` (items taken out of
    records that were kept) come from the client and are shown apart."""

    selection: Selection
    counts: UpsertCounts
    skipped: Counter[SkipReason] = field(default_factory=Counter)
    dropped: Counter[DropReason] = field(default_factory=Counter)

    @property
    def near_duplicates(self) -> int:
        return self.selection.near_duplicates + self.counts.near_duplicates

    def lines(self) -> list[str]:
        selection, counts = self.selection, self.counts
        lines = [
            f"titles added: {counts.added}",
            f"titles updated: {counts.updated}",
            f"titles unchanged: {counts.unchanged}",
            f"kept as near-duplicates (not added): {self.near_duplicates}",
            f"refused to change, would break a stored game (AC-18): {counts.refused}",
            f"records skipped as invalid (AC-8): {sum(self.skipped.values())}"
            + _breakdown(self.skipped),
            f"items dropped from records that were kept: {sum(self.dropped.values())}"
            + _breakdown(self.dropped),
            f"records out of scope: {selection.out_of_scope}",
            f"records with a name that does not fit the list: "
            f"{sum(selection.name_problems.values())}" + _breakdown(selection.name_problems),
        ]
        if counts.refused_title_ids:
            shown = ", ".join(str(title_id) for title_id in counts.refused_title_ids[:20])
            more = len(counts.refused_title_ids) - 20
            lines.append(f"refused title ids: {shown}" + (f" and {more} more" if more > 0 else ""))
        return lines


def _breakdown(counter: Counter[Any]) -> str:
    parts = [f"{reason}: {count}" for reason, count in sorted(counter.items()) if count]
    return f" ({', '.join(parts)})" if parts else ""
