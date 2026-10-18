from pathlib import Path

import pytest
from platformdirs import windows
from platformdirs.macos import MacOS
from platformdirs.unix import Unix
from platformdirs.windows import Windows

from aftertrace.platform import paths


def test_native_application_data_path_is_usable(tmp_path, monkeypatch):
    native = paths.application_data_dir()
    assert isinstance(native, Path) and native.is_absolute()
    assert native.name == "AfterTrace"
    monkeypatch.setattr(paths, "application_data_dir", lambda: tmp_path / "AfterTrace")
    resolved = paths.database_path()
    resolved.parent.mkdir(parents=True)
    resolved.write_bytes(b"path test")
    assert resolved.read_bytes() == b"path test"


@pytest.mark.parametrize("platform_type", [Unix, MacOS, Windows])
def test_platform_resolution_is_delegated(tmp_path, monkeypatch, platform_type):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    if platform_type is Windows:
        # Only replace the Windows OS folder query, which cannot execute on Linux/macOS.
        monkeypatch.setattr(windows, "get_win_folder", lambda _: str(tmp_path / "local"))
    platform = platform_type("AfterTrace", appauthor=False, ensure_exists=False)
    calls = []

    def resolve(appname, *, appauthor, ensure_exists):
        calls.append((appname, appauthor, ensure_exists))
        return platform.user_data_path

    monkeypatch.setattr(paths, "user_data_path", resolve)
    assert paths.application_data_dir() == platform.user_data_path
    assert calls == [("AfterTrace", False, False)]


def test_codex_home_default_override_and_tilde_are_platform_owned(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.codex_history_directories() == (
        tmp_path / ".codex" / "sessions",
        tmp_path / ".codex" / "archived_sessions",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom home"))
    assert paths.codex_history_directories()[0] == tmp_path / "custom home" / "sessions"
    monkeypatch.setenv("CODEX_HOME", "")
    assert paths.codex_history_directories()[0] == tmp_path / ".codex" / "sessions"
    monkeypatch.setenv("CODEX_HOME", "~/codex-custom")
    # expanduser uses the OS home environment, not the Path.home class method.
    assert paths.codex_history_directories()[0] == Path("~/codex-custom").expanduser() / "sessions"
    assert list(tmp_path.iterdir()) == []
