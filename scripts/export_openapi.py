"""Write the app's OpenAPI document to openapi/openapi.json (the contract snapshot).

    uv run python scripts/export_openapi.py          # rewrite the snapshot
    uv run python scripts/export_openapi.py --check  # exit 1 if the snapshot is stale

The frontend generates its API types from this file (see ../CONTRACTS.md).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / "openapi" / "openapi.json"


def render() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        if not SNAPSHOT.exists() or SNAPSHOT.read_text(encoding="utf-8") != rendered:
            print("openapi/openapi.json is stale: run scripts/export_openapi.py and commit it")
            return 1
        return 0
    SNAPSHOT.write_text(rendered, encoding="utf-8")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
