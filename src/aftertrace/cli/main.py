"""Small foundation CLI; no history access or model calls."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from aftertrace import __version__
from aftertrace.core.errors import AfterTraceError
from aftertrace.services.database import initialize_database


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AfterTrace local-first desktop foundation")
    parser.add_argument("--version", action="version", version=f"AfterTrace {__version__}")
    parser.add_argument(
        "--init-db",
        type=Path,
        metavar="PATH",
        help="initialize an AfterTrace-owned SQLite database at PATH",
    )
    args = parser.parse_args(argv)
    if args.init_db is not None:
        try:
            version = initialize_database(args.init_db)
        except AfterTraceError as error:
            parser.exit(1, f"AfterTrace: {error}\n")
        print(f"AfterTrace database ready (schema {version}).")
    else:
        print(f"AfterTrace {__version__} · Local Only")
        print("Foundation ready. Launch aftertrace-gui for the desktop shell.")
    return 0
