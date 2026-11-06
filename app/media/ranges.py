"""Reading the `Range` header of a media request (AC-72b, RFC 9110 sections 5.6.1 and 14.2).

Starlette's `FileResponse` serves one valid range with a 206, but it answers a malformed header with
a plain-text 400 and an unsatisfiable one with a plain-text 416 that has no body of ours. So the
route reads the header first with the rules below and only hands `FileResponse` a header it will
accept.
"""

import re
from enum import StrEnum

# `first-last`, `first-` or `-suffix`. ASCII digits only (`\d` also takes other scripts), and at most
# 18 of them, which no file reaches and which keeps `int()` away from absurd input.
RANGE_SPEC = re.compile(
    r"(?:(?P<first>[0-9]{1,18})-(?P<last>[0-9]{0,18})|-(?P<suffix>[0-9]{1,18}))"
)


# A range unit is a token (RFC 9110 section 5.6.2).
RANGE_UNIT = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", re.ASCII)


class RangeKind(StrEnum):
    FULL = "full"  # no header, an unknown unit, or several ranges: the whole file with 200
    SINGLE = "single"  # one satisfiable range: 206, served by `FileResponse`
    NOT_SATISFIABLE = "not_satisfiable"  # malformed or outside the file: 416


def classify_range(header: str | None, size: int) -> RangeKind:
    """What to do with a `Range` header for a file of `size` bytes.

    A range unit other than `bytes` is ignored, so the whole file is sent (RFC 9110 section 14.2:
    "MUST ignore a Range header field that contains a range unit it does not understand").
    Empty list elements (`bytes=0-1,` and `bytes=,0-1`) are accepted and skipped (section 5.6.1).
    Several ranges give the whole file, which the RFC allows ("a server MAY ignore the Range
    header") and which spares us `multipart/byteranges`, that nothing here needs. A header with no
    `=`, no unit, no range at all, a range that is not `first-last`, `first-` or `-suffix`, a
    `last` below `first`, or ranges that all start past the end is not satisfiable.
    """
    if header is None:
        return RangeKind.FULL
    unit, separator, specs = header.partition("=")
    unit = unit.strip().lower()
    if not separator or RANGE_UNIT.fullmatch(unit) is None:
        return RangeKind.NOT_SATISFIABLE
    if unit != "bytes":
        return RangeKind.FULL

    parts = [part for part in (part.strip() for part in specs.split(",")) if part]
    if not parts:
        return RangeKind.NOT_SATISFIABLE
    satisfiable = 0
    for part in parts:
        match = RANGE_SPEC.fullmatch(part)
        if match is None:
            return RangeKind.NOT_SATISFIABLE
        if match["suffix"] is not None:
            # `-0` asks for no bytes at all, and nothing can be taken from an empty file.
            is_satisfiable = int(match["suffix"]) > 0 and size > 0
        else:
            first = int(match["first"])
            if match["last"] and int(match["last"]) < first:
                return RangeKind.NOT_SATISFIABLE
            is_satisfiable = first < size
        satisfiable += is_satisfiable
    if satisfiable == 0:
        return RangeKind.NOT_SATISFIABLE
    return RangeKind.SINGLE if len(parts) == 1 else RangeKind.FULL
