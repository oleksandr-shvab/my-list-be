from enum import StrEnum


class ProgressStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    WON = "won"
    LOST = "lost"


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
