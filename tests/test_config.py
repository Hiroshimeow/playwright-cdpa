from __future__ import annotations

from pathlib import Path

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.errors import InvalidInputError


def test_cdp_endpoint_must_be_loopback() -> None:
    with pytest.raises(InvalidInputError):
        CoreConfig(cdp_endpoint="http://example.com:9222").validated()
    assert CoreConfig(cdp_endpoint="http://127.0.0.1:9222").validated()


def test_default_deployment_namespace_normalizes_loopback_aliases(tmp_path) -> None:
    first = CoreConfig(
        cdp_endpoint="http://127.0.0.1:9222/",
        coordination_dir=tmp_path,
    ).validated()
    second = CoreConfig(
        cdp_endpoint="http://localhost:9222",
        coordination_dir=tmp_path,
    ).validated()

    assert first.coordination_root == second.coordination_root
    assert first.coordination_root.parent == Path(tmp_path)


def test_explicit_deployment_id_is_bounded_and_path_safe(tmp_path) -> None:
    config = CoreConfig(
        coordination_dir=tmp_path,
        deployment_id="profile-9222",
    ).validated()
    assert config.coordination_root == tmp_path / "profile-9222"

    with pytest.raises(InvalidInputError):
        CoreConfig(coordination_dir=tmp_path, deployment_id="../escape").validated()


def test_relative_xdg_state_home_uses_cwd_independent_absolute_default(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    home.mkdir()
    repo_a.mkdir()
    repo_b.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", "relative-xdg-state")

    monkeypatch.chdir(repo_a)
    first = CoreConfig().validated()
    first_root = first.coordination_root

    monkeypatch.chdir(repo_b)
    second = CoreConfig().validated()

    assert first_root.is_absolute()
    assert second.coordination_root == first_root
    assert first_root.parent.parent.parent == home / ".local" / "state"

    monkeypatch.chdir(tmp_path)
    assert first.coordination_root == first_root


def test_relative_explicit_coordination_directory_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="absolute path"):
        CoreConfig(coordination_dir=Path("shared-coordination")).validated()


def test_same_relative_coordination_argument_is_rejected_from_different_cwds(
    tmp_path, monkeypatch
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    for repository in (repo_a, repo_b):
        monkeypatch.chdir(repository)
        with pytest.raises(InvalidInputError, match="absolute path"):
            CoreConfig(
                coordination_dir=Path("shared-coordination"),
                deployment_id="same-browser",
            ).validated()
        assert not (repository / "shared-coordination").exists()
