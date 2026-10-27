from fastapi.routing import APIRoute


def operation_id(route: APIRoute) -> str:
    """Stable, readable operation ids (auth_login, health_readyz): the frontend's generated
    function names come from them, so renaming a route handler is an API change."""
    tag = route.tags[0] if route.tags else "api"
    return f"{tag}_{route.name}"
