"""Fill the title list from IGDB (SPEC-04 AC-10 to AC-19).

Two steps. `select_records` reads the games from IGDB (any async iterable of `IgdbGame`) and keeps
every record the list could use; it touches no database. `upsert_titles` plans the whole run
against the database, then writes it. It matches stored titles by IGDB id, never deletes anything
(AC-17), picks one title for each normalized name and year (AC-14, and the plan's "never replace a
stored title") and leaves a title unchanged, with a count, when the change could break a stored
game (AC-18, R-15). A stopped run keeps its committed batches and a run again continues (AC-16).
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
    find_key_holders,
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
    then the override list. A name that is empty after cleaning, or too long for the column (raw or
    normalized), is passed over for the next one."""
    candidates: list[tuple[FranchiseSource, IgdbNamed]] = []
    if game.franchise is not None:
        candidates.append((FranchiseSource.FRANCHISE, game.franchise))
    candidates.extend(
        (FranchiseSource.COLLECTION, item) for item in sorted(game.collections, key=lambda c: c.id)
    )
    for source, item in candidates:
        name = FRANCHISE_OVERRIDES.get((source, item.id)) or clean_franchise(item.name)
        if not name or len(name) > MAX_FRANCHISE_LENGTH:
            continue
        # NFKD can make a name up to 18 times longer; the normalized column holds 1024.
        normalized = normalize_text(name)
        if normalized and len(normalized) <= MAX_NORMALIZED_NAME_LENGTH:
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
    name_problems: Counter[NameProblem] = field(default_factory=Counter)


Key = tuple[str, int | None]


async def select_records(games: AsyncIterable[IgdbGame]) -> Selection:
    """Every in-scope game whose name fits the list, in IGDB id order. Records that share a
    normalized name and year are all kept: `upsert_titles` picks the one that becomes the title,
    because that depends on what is stored."""
    selection = Selection()
    async for game in games:
        if not in_scope(game):
            selection.out_of_scope += 1
            continue
        record = to_record(game)
        if isinstance(record, NameProblem):
            selection.name_problems[record] += 1
            continue
        selection.records.append(record)
    selection.records.sort(key=lambda record: record.igdb_id)
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
    # Commits made so far, so a caller that sees an error knows whether anything was kept.
    commits: int = 0


def _batches[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# The columns a stored game's replay reads (AC-18). The year and the display texts are not read.
REPLAY_COLUMNS = frozenset({"normalized_name", "normalized_franchise"})
KEY_COLUMNS = frozenset({"normalized_name", "year"})


def _key_of(title: Title) -> Key:
    return (title.normalized_name, title.year)


def _best(records: Sequence[ImportRecord]) -> ImportRecord:
    """AC-14: more ratings, then the lower IGDB id."""
    return max(records, key=lambda record: (record.ratings, -record.igdb_id))


@dataclass
class KeyPlan:
    blocked: set[int]  # IGDB ids of stored titles whose rename is not made
    to_add: list[ImportRecord]
    skipped_new: int  # new records left out because their name and year are taken


def plan_keys(
    groups: dict[Key, list[ImportRecord]],
    titles: dict[int, Title],
    *,
    renamers: set[int],
    refused: set[int],
    outside: set[Key],
) -> KeyPlan:
    """Decide which record becomes the title of each normalized name and year.

    A key is taken when a title that stays holds it: a stored title outside the run (`outside`), a
    refused one or a blocked one (they keep their old key), or a stored title of the run that
    already has it. Then nothing is added for the key and a stored title that wants it is blocked.
    A stored title is never replaced (the plan's M1-21 step 3). On a free key a stored title that
    takes it wins over new records; among the contenders of one kind more ratings win, then the
    lower IGDB id (AC-14). A title that is renamed away frees its old key. A blocked title keeps its
    old key, which can block another rename, so this repeats until no new title is blocked."""
    blocked: set[int] = set()
    while True:
        fixed = outside | {_key_of(titles[igdb_id]) for igdb_id in refused | blocked}
        found = set(blocked)
        to_add: list[ImportRecord] = []
        skipped_new = 0
        for key, members in groups.items():
            stored = [record for record in members if record.igdb_id in titles]
            new = [record for record in members if record.igdb_id not in titles]
            takers = [
                record
                for record in stored
                if record.igdb_id in renamers and record.igdb_id not in blocked
            ]
            keepers = [
                record
                for record in stored
                if record.igdb_id not in renamers and record.igdb_id not in refused
            ]
            if key in fixed or keepers:
                found |= {record.igdb_id for record in takers}
                skipped_new += len(new)
            elif takers:
                winner = _best(takers)
                found |= {record.igdb_id for record in takers if record is not winner}
                skipped_new += len(new)
            elif new:
                to_add.append(_best(new))
                skipped_new += len(new) - 1
        if found == blocked:
            return KeyPlan(blocked=blocked, to_add=to_add, skipped_new=skipped_new)
        blocked = found


def _partial_fields(title: Title, record: ImportRecord) -> dict[str, Any]:
    """What a blocked rename still writes: the franchise, and a display change of the name that
    leaves the normalized name as it is. The name's identity and the year stay."""
    same_name = record.normalized_name == title.normalized_name
    return {
        "name": record.name if same_name else title.name,
        "franchise": record.franchise,
        "year": title.year,
    }


async def upsert_titles(
    db: AsyncSession,
    records: Sequence[ImportRecord],
    *,
    batch_size: int = BATCH_SIZE,
    counts: UpsertCounts | None = None,
) -> UpsertCounts:
    """Write the records. A stored title is matched by IGDB id; one that is not in `records` is
    never touched, let alone deleted (AC-17).

    The whole run is planned before the first write, so the result is the same whatever the order
    of the records or the size of a batch, and a second run changes nothing:
    1. A stored title with a change that AC-18 refuses stays wholly as it is (R-15).
    2. `plan_keys` picks the title of each name and year. A rename it blocks still writes the parts
       that do not collide (`_partial_fields`).
    3. All updates are written in one transaction, so a chain or a swap of names is never half
       committed. Then the new titles, a batch at a time with a commit after each (AC-16).
    Pass `counts` to read the commits made when an error stops the run."""
    counts = UpsertCounts() if counts is None else counts
    by_id = {record.igdb_id: record for record in records}
    titles: dict[int, Title] = {}
    for ids in _batches(list(by_id), batch_size):
        titles.update(await get_titles_by_igdb_ids(db, ids))

    changes: dict[int, frozenset[str]] = {}
    for igdb_id, title in titles.items():
        record = by_id[igdb_id]
        found = title_changes(title, name=record.name, franchise=record.franchise, year=record.year)
        if found:
            changes[igdb_id] = found
        else:
            counts.unchanged += 1

    replay_ids = [titles[i].id for i, found in changes.items() if found & REPLAY_COLUMNS]
    protected: set[int] = set()
    for ids in _batches(replay_ids, batch_size):
        protected |= await referenced_title_ids(db, ids)
    refused = {i for i in changes if titles[i].id in protected}
    counts.refused = len(refused)
    counts.refused_title_ids = sorted(titles[i].id for i in refused)

    renamers = {i for i, found in changes.items() if i not in refused and found & KEY_COLUMNS}
    groups: dict[Key, list[ImportRecord]] = {}
    for record in records:
        groups.setdefault(record.key, []).append(record)
    # Only the keys a rename or a new record can collide on are looked up.
    wanted = [
        key
        for key, members in groups.items()
        if any(m.igdb_id in renamers or m.igdb_id not in titles for m in members)
    ]
    outside: set[Key] = set()
    for keys in _batches(wanted, batch_size):
        for igdb_id, name, year in await find_key_holders(db, kind=TitleKind.VIDEO_GAME, keys=keys):
            if igdb_id is None or igdb_id not in by_id:
                outside.add((name, year))
    plan = plan_keys(groups, titles, renamers=renamers, refused=refused, outside=outside)
    counts.near_duplicates = len(plan.blocked) + plan.skipped_new

    is_written = False
    for igdb_id in changes:
        if igdb_id in refused:
            continue
        title, record = titles[igdb_id], by_id[igdb_id]
        fields = (
            _partial_fields(title, record)
            if igdb_id in plan.blocked
            else {"name": record.name, "franchise": record.franchise, "year": record.year}
        )
        if title_changes(title, **fields):
            await update_title(db, title, flush=False, **fields)
            counts.updated += 1
            is_written = True
    if is_written:
        await db.commit()
        counts.commits += 1

    for batch in _batches(plan.to_add, batch_size):
        for record in batch:
            await create_title(
                db,
                name=record.name,
                franchise=record.franchise,
                year=record.year,
                igdb_id=record.igdb_id,
                flush=False,
            )
        await db.commit()
        counts.commits += 1
        counts.added += len(batch)
    return counts


@dataclass
class ImportReport:
    """AC-19. `skipped` (whole records IGDB's data failed) and `dropped` (items taken out of
    records that were kept) come from the client and are shown apart."""

    selection: Selection
    counts: UpsertCounts
    skipped: Counter[SkipReason] = field(default_factory=Counter)
    dropped: Counter[DropReason] = field(default_factory=Counter)

    def lines(self) -> list[str]:
        selection, counts = self.selection, self.counts
        lines = [
            f"titles added: {counts.added}",
            f"titles updated: {counts.updated}",
            f"titles unchanged: {counts.unchanged}",
            f"kept as near-duplicates (not added): {counts.near_duplicates}",
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
