import importlib.metadata
import subprocess
import sys

import aftertrace
from aftertrace.cli.main import main as cli_main
from aftertrace.ui.app import main as gui_main


def test_import_and_installed_entry_points():
    assert aftertrace.__version__ == importlib.metadata.version("aftertrace")
    entries = importlib.metadata.distribution("aftertrace").entry_points
    targets = {entry.name: entry.load() for entry in entries}
    assert targets["aftertrace"] is cli_main
    assert targets["aftertrace-gui"] is gui_main


def test_cli_default_version_and_database(tmp_path, capsys):
    assert cli_main([]) == 0
    assert "Local Only" in capsys.readouterr().out
    path = tmp_path / "app.sqlite3"
    assert cli_main(["--init-db", str(path)]) == 0
    assert "schema 1" in capsys.readouterr().out
    result = subprocess.run(
        [sys.executable, "-m", "aftertrace", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert aftertrace.__version__ in result.stdout
