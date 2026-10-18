"""Centralized application-owned paths; resolving a path does not create it."""

import os
from pathlib import Path

from platformdirs import user_data_path


def application_data_dir() -> Path:
    return user_data_path("AfterTrace", appauthor=False, ensure_exists=False)


def database_path() -> Path:
    return application_data_dir() / "aftertrace.sqlite3"


def codex_history_directories() -> tuple[Path, Path]:
    """Codex's documented home override and active/archive rollout locations.

    Resolve candidates only; never create or modify a source directory.
    Path.home() uses the native user home on Windows, macOS, and Linux.
    """
    configured = os.environ.get("CODEX_HOME")
    codex_root = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return codex_root / "sessions", codex_root / "archived_sessions"


def publisher_logo_path() -> Path | None:
    # Optional source-checkout assets. Installed shells gracefully use publisher text.
    branding = Path(__file__).resolve().parents[3] / "assets" / "branding"
    for name in ("TaskPuppyLogo.png", "taskpuppy-kreations.png"):
        candidate = branding / name
        if candidate.is_file():
            return candidate
    return None
