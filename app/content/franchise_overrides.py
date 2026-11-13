"""Corrections for franchise names that IGDB spells badly (AC-12).

Keyed by where the name came from and its IGDB id, because a franchise and a collection (series)
can share an id. The value is the name the title list shows, used as is. Keep this list short, and
hold no puzzle answers in it: it is code and goes to Git.
"""

from enum import StrEnum


class FranchiseSource(StrEnum):
    FRANCHISE = "franchise"
    COLLECTION = "collection"


FRANCHISE_OVERRIDES: dict[tuple[FranchiseSource, int], str] = {}
