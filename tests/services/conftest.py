import json
import os
import subprocess
from pathlib import Path

import pytest

from aftertrace.services.workspace import WorkspaceService

FIXTURE = Path(__file__).parents[1] / "fixtures" / "codex" / "normal.jsonl"


def git_repository(path, remote=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "--quiet", str(path)],
        env=environment,
        check=True,
        capture_output=True,
    )
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "config", "remote.origin.url", remote],
            env=environment,
            check=True,
            capture_output=True,
        )
    return path


def write_session(directory, cwd, number=1, text=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{number:03d}.jsonl"
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    rows[0]["payload"].update(id=f"synthetic-{number}", cwd=str(cwd) if cwd else None)
    for row in rows:
        payload = row["payload"]
        if row["type"] == "turn_context":
            payload["cwd"] = str(cwd) if cwd else None
        if text and row["type"] == "event_msg" and payload.get("type") == "user_message":
            payload["message"] = text
        if text and row["type"] == "response_item" and payload.get("role") == "user":
            payload["content"][0]["text"] = text
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    repo = git_repository(tmp_path / "project", "https://example.invalid/team/example.git")
    source = home / "sessions"
    source.mkdir(parents=True)
    return WorkspaceService(tmp_path / "aftertrace.sqlite3"), repo, source
