"""Centralized application-owned paths; resolving a path does not create it."""

from pathlib import Path

from platformdirs import user_data_path


def application_data_dir() -> Path:
    return user_data_path("AfterTrace", appauthor=False, ensure_exists=False)


def database_path() -> Path:
    return application_data_dir() / "aftertrace.sqlite3"


def publisher_logo_path() -> Path | None:
    # Optional source-checkout assets. Installed shells gracefully use publisher text.
    branding = Path(__file__).resolve().parents[3] / "assets" / "branding"
    for name in ("TaskPuppyLogo.png", "taskpuppy-kreations.png"):
        candidate = branding / name
        if candidate.is_file():
            return candidate
    return None
