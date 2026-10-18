"""Read-only Git identity and cross-platform location evidence."""

import ntpath
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


def normalize_location(value: str | None) -> str | None:
    if not value:
        return None
    if PureWindowsPath(value).is_absolute():
        return "windows:" + ntpath.normcase(ntpath.normpath(value))
    if value.startswith("/"):
        return "posix:" + posixpath.normpath(value)
    return None


def contains_location(root: str, value: str) -> bool:
    parent, child = normalize_location(root), normalize_location(value)
    if parent is None or child is None:
        return False
    separator = "\\" if parent.startswith("windows:") else "/"
    return child == parent or child.startswith(parent.rstrip(separator) + separator)


def normalize_remote(value: str) -> str | None:
    """Normalize common fetch URLs without retaining credentials or assuming path case."""
    if "://" not in value:
        match = re.fullmatch(r"(?:[^/@:]+@)?([\w.-]+):(.+)", value)
        if not match or len(match[1]) == 1:  # A Windows drive is not a host.
            return None
        value = f"ssh://{match[1]}/{match[2]}"
    try:
        url = urlsplit(value)
        if url.scheme not in ("https", "http", "ssh", "git") or not url.hostname:
            return None
        host = url.hostname.lower()
        port = url.port
        if port and port != {"ssh": 22, "http": 80, "https": 443, "git": 9418}[url.scheme]:
            host += f":{port}"
        path = url.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not path or any(part in (".", "..", "") for part in path.split("/")):
            return None
        return f"{host}/{path}"
    except ValueError:
        return None


@dataclass(frozen=True)
class ProjectIdentity:
    canonical_identity: str
    display_name: str
    root: str
    git_remote: str | None
    kind: str


def _git(directory: Path, *args: str) -> str | None:
    # No global config, injected GIT_* overrides, hooks, network, index refresh, or optional locks.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0"
    )
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(directory), *args],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def identify_project(hint: str | None) -> ProjectIdentity | None:
    """An accessible Git repository supplies evidence; a path/name alone does not."""
    if normalize_location(hint) is None:
        return None
    path = Path(hint)
    if not path.is_absolute() or not path.is_dir():
        return None
    roots = _git(path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
    if roots is None or len(roots.splitlines()) != 2:
        return None
    root, common_dir = roots.splitlines()
    urls = _git(path, "config", "--local", "--get-all", "remote.origin.url")
    remotes = {normalize_remote(url) for url in urls.splitlines()} if urls else set()
    remote = next(iter(remotes)) if len(remotes) == 1 else None
    if normalize_location(common_dir) is None:
        return None
    # A Git directory's local filesystem identity survives renames. Merely reusing a path
    # does not establish the same repository. This anchor makes no cross-machine claim.
    if remote:
        canonical = f"git-remote:{remote}"
    else:
        try:
            info = Path(common_dir).stat()
        except OSError:
            return None
        if not info.st_ino:
            return None
        canonical = f"git-local:{info.st_dev}:{info.st_ino}"
    return ProjectIdentity(
        canonical, Path(root).name, root, remote, "git_remote" if remote else "git_local"
    )
