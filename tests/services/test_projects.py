from pathlib import Path

import pytest

from aftertrace.platform import projects
from aftertrace.platform.projects import identify_project, normalize_location, normalize_remote

from .conftest import git_repository


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.invalid/Team/Repo.git",
        "https://token:password@example.invalid/Team/Repo/",
        "git@EXAMPLE.INVALID:Team/Repo.git",
        "ssh://git@example.invalid:22/Team/Repo.git",
    ],
)
def test_supported_remote_representations_remove_credentials(remote):
    assert normalize_remote(remote) == "example.invalid/Team/Repo"


@pytest.mark.parametrize(
    "remote", ["/some/local/repo", r"D:\Projects\Repo", "file:///repo", "https://"]
)
def test_unsupported_remote_evidence_is_not_a_cross_machine_identity(remote):
    assert normalize_remote(remote) is None


def test_custom_ports_and_repository_path_case_are_not_aggressively_merged():
    assert normalize_remote("ssh://git@example.invalid:443/team/repo") != normalize_remote(
        "https://example.invalid/team/repo"
    )
    assert normalize_remote("https://example.invalid/Team/Repo") != normalize_remote(
        "https://example.invalid/team/repo"
    )


def test_git_inspection_is_read_only_and_subdirectories_share_identity(tmp_path):
    repo = git_repository(tmp_path / "repo", "git@example.invalid:team/repo.git")
    child = repo / "src"
    child.mkdir()
    before = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in (repo / ".git").rglob("*")
        if path.is_file()
    }
    identity = identify_project(str(repo))
    nested = identify_project(str(child))
    assert identity == nested
    assert identity.canonical_identity == "git-remote:example.invalid/team/repo"
    assert identity.root == str(repo)
    after = {
        str(path.relative_to(repo)): path.read_bytes()
        for path in (repo / ".git").rglob("*")
        if path.is_file()
    }
    assert before == after


def test_non_git_inaccessible_and_foreign_paths_remain_unclassified(tmp_path, monkeypatch):
    assert identify_project(str(tmp_path)) is None
    assert identify_project(str(tmp_path / "missing")) is None
    if Path("/").is_absolute():
        assert identify_project(r"Z:\nonexistent\project") is None
    monkeypatch.setattr(projects, "_git", lambda *args: None)
    assert identify_project(str(tmp_path)) is None
    assert normalize_location(r"D:\Projects\Foo") == normalize_location(r"d:\projects\foo")
    assert normalize_location("/Users/example/Foo") != normalize_location("/home/example/Foo")


def test_git_without_remote_is_local_evidence_and_separate_repositories_stay_separate(tmp_path):
    first = git_repository(tmp_path / "first")
    second = git_repository(tmp_path / "second")
    a, b = identify_project(str(first)), identify_project(str(second))
    assert a.kind == b.kind == "git_local"
    assert a.canonical_identity != b.canonical_identity


def test_local_repository_identity_uses_git_metadata_and_survives_a_local_rename(tmp_path):
    original = git_repository(tmp_path / "original")
    before = identify_project(str(original))
    moved = tmp_path / "moved"
    original.rename(moved)
    after = identify_project(str(moved))
    assert before.canonical_identity == after.canonical_identity
    assert str(original) not in before.canonical_identity
