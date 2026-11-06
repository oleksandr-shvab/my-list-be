from fastapi.routing import APIRoute


def operation_id(route: APIRoute) -> str:
    """Ids like `auth_login`. The frontend's generated function names come from them, so renaming
    a handler is an API change."""
    tag = route.tags[0] if route.tags else "api"
    return f"{tag}_{route.name}"
