import pytest


@pytest.fixture(autouse=True)
def isolated_workspace_database(tmp_path, monkeypatch):
    # Even default-window and entry-point smoke tests only use temporary AfterTrace state.
    monkeypatch.setattr(
        "aftertrace.services.workspace.database_path", lambda: tmp_path / "app.sqlite3"
    )
