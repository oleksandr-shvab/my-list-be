"""The review page's HTML. There is no template engine: every text that is not written here goes
through `esc`, the one escaping helper. IGDB's names, genres, platforms and developers are
untrusted. The answer is shown on purpose (the reviewer needs it), but escaped like the rest.
"""

import html as stdlib_html
from dataclasses import dataclass
from urllib.parse import urlsplit

STYLESHEET_PATH = "/review.css"
FILES_PATH = "/files"

HINT_LABELS = {
    "critic_score": "Critic score",
    "platform": "Platform",
    "genre": "Genre",
    "year": "Release year",
    "developer": "Developer",
}

CHECKLIST = (
    "The answer is a recognisable game",
    "Step 1 is readable but hard",
    "No clue shows the game's name, logo or a character name",
    "The hints are correct",
    "The clip plays (it may reuse the stills when the game has few screenshots)",
)

CLIP_FROM_STILLS = " · Clip made from the stills (decision 74)"
NO_REBUILD_NOTE = "This game has too few screenshots to rebuild; reject as Never"

# One screen at 1280 by 800 (AC-54): the six steps as a grid on the left, the answer and the
# actions in a column on the right. The page has no script and no inline style, so the style is
# served from `/review.css` (the CSP is `default-src 'self'`).
STYLESHEET = """
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.35 system-ui, sans-serif; background: #15171a; color: #e8e8e8; }
a { color: #8ab4ff; }
header { display: flex; justify-content: space-between; align-items: center; height: 40px;
  padding: 0 12px; background: #1f2227; }
header h1 { font-size: 15px; margin: 0; }
.layout { display: grid; grid-template-columns: 1fr 340px; gap: 12px; padding: 12px;
  height: calc(100vh - 40px); }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; align-content: start; }
figure { margin: 0; background: #1f2227; padding: 6px; border-radius: 4px; }
figure img, figure video { display: block; width: 100%; height: auto; background: #000; }
figcaption { margin-top: 4px; font-size: 12px; }
aside { display: flex; flex-direction: column; gap: 10px; }
aside section { background: #1f2227; padding: 10px; border-radius: 4px; }
aside h2 { font-size: 13px; margin: 0 0 6px; text-transform: uppercase; color: #9aa; }
.answer { font-size: 18px; font-weight: 600; }
ul.checklist { margin: 0; padding-left: 0; list-style: none; }
.actions { display: flex; gap: 8px; }
button { font: inherit; padding: 8px 12px; border-radius: 4px; border: 1px solid #556;
  background: #2b2f36; color: inherit; cursor: pointer; }
button.approve { background: #1f6f3f; border-color: #2a9d5c; flex: 1; }
button.rebuild { background: #7a5b17; border-color: #b0862a; flex: 1; }
button.never { background: #7a2323; border-color: #b03a3a; flex: 1; }
.missing { padding: 24px 6px; text-align: center; color: #e88; }
table { border-collapse: collapse; width: 100%; }
td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid #2d3138; }
main.page { padding: 12px; }
"""


def esc(value: object) -> str:
    """The only way a value reaches the page. Escapes quotes too, so it is safe in an attribute."""
    return stdlib_html.escape(str(value), quote=True)


def link_target(url: str | None) -> str | None:
    """`url` if it is a plain http(s) URL, else None: a `javascript:` or `data:` link is never
    made, whatever IGDB data got into the column."""
    if not url:
        return None
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{esc(title)}</title>"
        f'<link rel="stylesheet" href="{STYLESHEET_PATH}"></head><body>{body}</body></html>'
    )


@dataclass(frozen=True)
class QueueItem:
    puzzle_id: int
    build_slot: int | None
    answer_name: str


def queue_page(items: list[QueueItem]) -> str:
    rows = "".join(
        f"<tr><td>{esc(item.build_slot if item.build_slot is not None else '-')}</td>"
        f'<td><a href="/drafts/{esc(item.puzzle_id)}">{esc(item.answer_name)}</a></td></tr>'
        for item in items
    )
    table = (
        f"<table><tr><th>Slot</th><th>Answer</th></tr>{rows}</table>"
        if items
        else "<p>Nothing to review.</p>"
    )
    body = (
        f"<header><h1>Drafts in review: {esc(len(items))}</h1></header>"
        f'<main class="page">{table}</main>'
    )
    return page("Review", body)


@dataclass(frozen=True)
class FileView:
    key: str
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class ClueView:
    step: int
    is_video: bool
    hint_kind: str | None
    hint_value: str | None
    source_url: str | None
    source: FileView | None
    poster: FileView | None


@dataclass(frozen=True)
class DraftView:
    puzzle_id: int
    build_slot: int | None
    answer_name: str
    answer_year: int | None
    answer_franchise: str | None
    clues: list[ClueView]
    queue_size: int
    can_rebuild: bool = True  # False: the game has too few screenshots (decision 74)
    clip_from_stills: bool = False  # step 6 reuses steps 2 to 5's screenshots


def _file_url(file: FileView) -> str:
    return f"{FILES_PATH}/{esc(file.key)}"


def _clue_media(clue: ClueView) -> str:
    if clue.source is None:
        return '<div class="missing">no file</div>'
    source = clue.source
    if clue.is_video:
        poster = f' poster="{_file_url(clue.poster)}"' if clue.poster is not None else ""
        return (
            f"<video muted loop autoplay playsinline controls{poster}"
            f' width="{esc(source.width)}" height="{esc(source.height)}">'
            f'<source src="{_file_url(source)}" type="{esc(source.content_type)}"></video>'
        )
    return (
        f'<a href="{_file_url(source)}" target="_blank" rel="noopener">'
        f'<img src="{_file_url(source)}" alt="Step {esc(clue.step)}"'
        f' width="{esc(source.width)}" height="{esc(source.height)}"></a>'
    )


def _clue_caption(clue: ClueView) -> str:
    parts = [f"<strong>Step {esc(clue.step)}</strong>"]
    if clue.hint_kind is not None and clue.hint_value is not None:
        label = HINT_LABELS.get(clue.hint_kind, "Hint")
        parts.append(f"{esc(label)}: {esc(clue.hint_value)}")
    else:
        parts.append("no hint")
    target = link_target(clue.source_url)
    if target is not None:
        parts.append(
            f'<a href="{esc(target)}" target="_blank" rel="noopener noreferrer">source</a>'
        )
    elif clue.source_url:
        parts.append(f"source: {esc(clue.source_url)}")
    return " · ".join(parts)


def draft_page(draft: DraftView, token: str) -> str:
    figures = "".join(
        f"<figure>{_clue_media(clue)}<figcaption>{_clue_caption(clue)}"
        f"{CLIP_FROM_STILLS if clue.step == 6 and draft.clip_from_stills else ''}"
        "</figcaption></figure>"
        for clue in draft.clues
    )
    rebuild = (
        '<button class="rebuild" type="submit" name="reason" value="rebuild">Rebuild</button>'
        if draft.can_rebuild
        else ""
    )
    no_rebuild = "" if draft.can_rebuild else f"<p>{esc(NO_REBUILD_NOTE)}</p>"
    year = esc(draft.answer_year) if draft.answer_year is not None else "no year"
    franchise = esc(draft.answer_franchise) if draft.answer_franchise else "no franchise"
    checklist = "".join(
        f'<li><label><input type="checkbox"> {esc(item)}</label></li>' for item in CHECKLIST
    )
    base = f"/drafts/{esc(draft.puzzle_id)}"
    hidden = f'<input type="hidden" name="token" value="{esc(token)}">'
    slot = str(draft.build_slot) if draft.build_slot is not None else "none"
    body = (
        f"<header><h1>Slot {esc(slot)}</h1>"
        f'<a href="/">{esc(draft.queue_size)} in review</a></header>'
        f'<div class="layout"><div class="grid">{figures}</div><aside>'
        f'<section><h2>Answer</h2><div class="answer">{esc(draft.answer_name)}</div>'
        f"<div>{year} · {franchise}</div></section>"
        f'<section><h2>Check</h2><ul class="checklist">{checklist}</ul></section>'
        f'<section><h2>Approve</h2><form method="post" action="{base}/approve">{hidden}'
        '<div class="actions"><button class="approve" type="submit">Approve</button></div>'
        "</form></section>"
        f'<section><h2>Reject</h2><form method="post" action="{base}/reject">{hidden}'
        f'<div class="actions">{rebuild}'
        '<button class="never" type="submit" name="reason" value="never">Never</button>'
        f"</div>{no_rebuild}</form></section></aside></div>"
    )
    return page(f"Slot {slot}", body)


def message_page(message: str) -> str:
    body = f'<header><h1>{esc(message)}</h1><a href="/">Back to the queue</a></header>'
    return page("Review", body)
