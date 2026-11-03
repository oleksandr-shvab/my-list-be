import pytest

from app.games.normalize import normalize_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Half-Life 2", "half life 2"),
        ("Pokémon", "pokemon"),
        ("Poke\u0301mon", "pokemon"),
        (
            "  The   Legend of Zelda:  Breath of the Wild!  ",
            "the legend of zelda breath of the wild",
        ),
        ("S.T.A.L.K.E.R.", "s t a l k e r"),
        ("Ｈａｌｆ－Ｌｉｆｅ　２", "half life 2"),
        ("ＤＯＯＭ", "doom"),
        ("Straße", "strasse"),
        ("ÅÄÖ", "aao"),
        ("Tab\tand\nnewline", "tab and newline"),
        ("Portal 2", "portal 2"),
        ("", ""),
        ("   ", ""),
        ("!?-.:", ""),
        ("！？", ""),
    ],
    ids=[
        "hyphen_to_space",
        "accent_dropped_precomposed",
        "accent_dropped_decomposed",
        "punctuation_collapsed_and_trimmed",
        "dotted_letters",
        "full_width_letters_digits_hyphen_space",
        "full_width_upper_case",
        "casefold_sharp_s",
        "several_accents",
        "whitespace_kinds",
        "already_normal",
        "empty",
        "only_spaces",
        "only_punctuation",
        "only_full_width_punctuation",
    ],
)
def test__normalize_text__table(text: str, expected: str) -> None:
    assert normalize_text(text) == expected


@pytest.mark.parametrize("text", ["Half-Life 2", "Pokémon", "Ｈａｌｆ－Ｌｉｆｅ", "  A -- B  "])
def test__normalize_text__is_idempotent(text: str) -> None:
    once = normalize_text(text)

    assert normalize_text(once) == once
