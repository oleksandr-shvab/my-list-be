import unicodedata


def normalize_text(text: str) -> str:
    """Normalize a title name or franchise for matching and search.

    NFKD, drop combining marks, casefold, turn every non-alphanumeric character into a space,
    collapse spaces, trim. "Half-Life 2" becomes "half life 2".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(
        "".join(char if char.isalnum() else " " for char in without_marks.casefold()).split()
    )
