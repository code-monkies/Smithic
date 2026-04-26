"""Config schema validation tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from smithic.config import RESERVED_LABELS, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "smithic.toml"
    config.write_text(textwrap.dedent(body), encoding="utf-8")
    return config


def test_minimal_config_with_inline_mission(tmp_path: Path) -> None:
    config = _write(
        tmp_path,
        """
        [target]
        path = "."
        mission_text = "Build a thing."
        """,
    )
    cfg, _ = load_config(config)
    assert cfg.target.resolve_mission(tmp_path) == "Build a thing."
    assert cfg.swarm.parallel_runs == 1
    assert cfg.budget.max_usd_per_run == 5.00


def test_mission_file_resolution(tmp_path: Path) -> None:
    (tmp_path / "MISSION.md").write_text("# my mission\n", encoding="utf-8")
    config = _write(
        tmp_path,
        """
        [target]
        path = "."
        mission = "./MISSION.md"
        """,
    )
    cfg, config_dir = load_config(config)
    assert cfg.target.resolve_mission(config_dir) == "# my mission"


def test_must_specify_exactly_one_mission_source(tmp_path: Path) -> None:
    config = _write(
        tmp_path,
        """
        [target]
        path = "."
        mission = "./MISSION.md"
        mission_text = "also inline"
        """,
    )
    with pytest.raises(ValidationError):
        load_config(config)


def test_reserved_labels_are_rejected(tmp_path: Path) -> None:
    sample = next(iter(RESERVED_LABELS))
    config = _write(
        tmp_path,
        f"""
        [target]
        path = "."
        mission_text = "x"

        [pr]
        labels = ["{sample}", "ok-label"]
        """,
    )
    with pytest.raises(ValidationError):
        load_config(config)


def test_extra_keys_are_rejected(tmp_path: Path) -> None:
    config = _write(
        tmp_path,
        """
        [target]
        path = "."
        mission_text = "x"
        unknown_field = true
        """,
    )
    with pytest.raises(ValidationError):
        load_config(config)
