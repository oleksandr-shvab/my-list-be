import json
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi" / "openapi.json"


def test__openapi_snapshot__matches_the_app() -> None:
    # The frontend generates its types from this file: regenerate it with
    # `uv run python scripts/export_openapi.py` whenever a route or schema changes.
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert committed == json.loads(json.dumps(app.openapi())), "openapi/openapi.json is stale"


def test__openapi__errors_use_problem_details_and_routes_are_versioned() -> None:
    document = app.openapi()

    assert all(
        path.startswith("/v1/") or path in ("/healthz", "/readyz") for path in document["paths"]
    )
    login = document["paths"]["/v1/auth/login"]["post"]
    assert login["operationId"] == "auth_login"
    assert login["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ProblemDetails"
    )
