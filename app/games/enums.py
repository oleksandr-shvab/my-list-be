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
