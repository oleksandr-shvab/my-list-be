# IGDB fixtures

Written by hand from the field list in the IGDB v4 API docs (https://api-docs.igdb.com/), with
made-up games. Nothing here is a recorded IGDB response (AC-43). After the owner has IGDB
credentials, run `scripts/igdb_list.py --limit 5` once and compare one real record with
`games_page_1.json`; if the shape differs, fix these files and `app/content/igdb.py` together.

- `token.json`: the Twitch token answer.
- `games_page_1.json`, `games_page_2.json`: two pages of a list (page size 2 in the tests); the
  first game of page 1 has every field.
- `games_invalid.json`: one record per way to be skipped (AC-8), one record with a good and a bad
  screenshot, and one good record.
- `games_lenient.json`: valid games with one bad nested item each (a genre with no name, a company
  given as a number, null lists, a platform with no name, a bad franchise); the game is kept and
  the item is dropped (AC-8).
- `games_import.json`: one page for the title import (M1-21): "Doom" 1993 twice and 2016 (AC-14), a
  series name to clean, a DLC, an edition and an unrated game (out of scope), a name with no letter
  (skipped by AC-8), a bad genre (dropped), a 300-character name and a game with no release date.
- `error_401.json`, `error_429.json`, `error_503.json`: error bodies.
