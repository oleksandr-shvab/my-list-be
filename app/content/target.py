"""The guard every script that writes data goes through (R-13, AC-44, AC-84).

A script is told where it writes with `--target <name>`. The name must equal the `ENVIRONMENT`
environment variable itself, so a script cannot write to production because `.env` says so. The
run prints where it will write before the first write. A target other than `local` also needs the
name typed again (`--confirm-target <name>`, or at a prompt), and no run is allowed in CI.

`refusal_reason` is the stricter rule of the scripts that delete data (`seed_dev.py`,
`bench_title_search.py`): local only.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import Settings, settings

LOCAL_TARGET = "local"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class TargetRefusedError(Exception):
    """The run must not write here. The message says why and holds no credential."""


def refusal_reason(*, needs_local_storage: bool = True) -> str | None:
    """Why a script that deletes data must not run here, or None. It checks the ENVIRONMENT
    variable itself: `settings.ENVIRONMENT` defaults to `local` when the variable is missing, which
    would make this check pass by accident. `needs_local_storage` is false for a script that
    writes no media."""
    environment = os.environ.get("ENVIRONMENT")
    if environment != LOCAL_TARGET:
        return f"the ENVIRONMENT variable is {environment!r}, not 'local'"
    if needs_local_storage and settings.MEDIA_STORAGE != "local":
        return f"MEDIA_STORAGE is {settings.MEDIA_STORAGE!r}, and the seed writes to a local folder"
    host = urlsplit(settings.DATABASE_URL).hostname
    if host not in LOCAL_HOSTS:
        return f"DATABASE_URL points at host {host!r}, not this machine"
    return None


@dataclass(frozen=True)
class Target:
    name: str
    database_host: str | None
    storage: str

    def describe(self) -> str:
        """The line printed before the first write: no password, key or credential in it."""
        return f"Target: {self.name}; database host: {self.database_host}; storage: {self.storage}"


def _storage_description(app_settings: Settings) -> str:
    if app_settings.MEDIA_STORAGE == "s3":
        return f"bucket {app_settings.S3_BUCKET}"
    return f"folder {app_settings.media_local_path}"


def check_target(
    target: str | None,
    confirm_target: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    app_settings: Settings | None = None,
    ask: Callable[[str], str] | None = None,
    needs_local_storage: bool = False,
) -> Target:
    """Return the target to write to, or raise `TargetRefusedError`. `ask` reads one line from a
    person; pass None when there is no one to ask, and a non-local target then needs
    `confirm_target`."""
    environ = os.environ if environ is None else environ
    app_settings = settings if app_settings is None else app_settings
    if environ.get("CI"):
        raise TargetRefusedError("the CI environment variable is set; scripts do not write in CI")
    if not target:
        raise TargetRefusedError("--target is required")
    environment = environ.get("ENVIRONMENT")
    if target != environment:
        raise TargetRefusedError(
            f"--target is {target!r}, but the ENVIRONMENT variable is {environment!r}"
        )
    host = urlsplit(app_settings.DATABASE_URL).hostname
    if target == LOCAL_TARGET:
        if host not in LOCAL_HOSTS:
            raise TargetRefusedError(f"DATABASE_URL points at host {host!r}, not this machine")
        if needs_local_storage and app_settings.MEDIA_STORAGE != "local":
            raise TargetRefusedError(f"MEDIA_STORAGE is {app_settings.MEDIA_STORAGE!r}")
    else:
        typed = confirm_target
        if typed is None and ask is not None:
            typed = ask(f"Type {target!r} to write to it: ").strip()
        if typed != target:
            raise TargetRefusedError(f"a target other than 'local' needs --confirm-target {target}")
    return Target(name=target, database_host=host, storage=_storage_description(app_settings))
