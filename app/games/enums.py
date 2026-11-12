from enum import StrEnum


class ProgressStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    WON = "won"
    LOST = "lost"


class ArchiveState(StrEnum):
    """What an account's list entry says about its game: the stored statuses plus a game that was
    never started."""

    NOT_PLAYED = "not_played"
    IN_PROGRESS = "in_progress"
    WON = "won"
    LOST = "lost"


class MergeRejectionCode(StrEnum):
    """Why a merge entry was left out (AC-59). The values are error codes of the API."""

    VARIANT_NOT_FOUND = "variant_not_found"
    PUZZLE_NOT_FOUND = "puzzle_not_found"
    INVALID_GUESS_HISTORY = "invalid_guess_history"


class GuessResult(StrEnum):
    CORRECT = "correct"
    WRONG = "wrong"
    FRANCHISE = "franchise"
    SKIPPED = "skipped"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class ClueRole(StrEnum):
    SOURCE = "source"
    POSTER = "poster"


class ClueContentType(StrEnum):
    IMAGE_WEBP = "image/webp"
    VIDEO_MP4 = "video/mp4"
    VIDEO_WEBM = "video/webm"


class ExclusionReason(StrEnum):
    """Why a title is held back as an answer: `rebuild` for one slot, `never` for good."""

    REBUILD = "rebuild"
    NEVER = "never"
