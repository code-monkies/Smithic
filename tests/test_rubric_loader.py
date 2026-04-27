"""Rubric loader + schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithic.rubric.loader import RubricError, load_rubric


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_default_rubric_loads_cleanly() -> None:
    rubric = load_rubric(None)
    assert "market_demand" in rubric.axes
    assert rubric.thresholds.min_total == 0.55
    assert rubric.thresholds.min_per_axis == 0.20
    # Default weights sum to 1.0 (validated by Rubric.model_validator).
    total = sum(axis.weight for axis in rubric.axes.values())
    assert 0.999 <= total <= 1.001


def test_user_override_replaces_threshold_only(tmp_path: Path) -> None:
    override = _write(
        tmp_path / "rubric.yaml",
        """
        thresholds:
          min_total: 0.70
        """,
    )
    rubric = load_rubric(override)
    assert rubric.thresholds.min_total == 0.70
    # Default axes preserved.
    assert "market_demand" in rubric.axes


def test_user_override_can_replace_axis_weight(tmp_path: Path) -> None:
    # Pump market_demand up; drop one of the others to keep weights summing to 1.0.
    override = _write(
        tmp_path / "rubric.yaml",
        """
        axes:
          market_demand:
            weight: 0.35
          reversibility:
            weight: 0.0
        """,
    )
    rubric = load_rubric(override)
    assert rubric.axes["market_demand"].weight == 0.35
    assert "reversibility" not in rubric.axes


def test_invalid_yaml_raises_clear_error(tmp_path: Path) -> None:
    bad = _write(tmp_path / "rubric.yaml", "axes: [nope: this isn't yaml")
    with pytest.raises(RubricError, match="not valid YAML"):
        load_rubric(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RubricError, match="not found"):
        load_rubric(tmp_path / "missing.yaml")


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    # Override that pushes the total out of range.
    override = _write(
        tmp_path / "rubric.yaml",
        """
        axes:
          market_demand:
            weight: 0.99
        """,
    )
    with pytest.raises(RubricError, match="weights"):
        load_rubric(override)


def test_as_prompt_block_renders_rubric() -> None:
    rubric = load_rubric(None)
    block = rubric.as_prompt_block()
    assert "Scoring rubric" in block
    assert "market_demand" in block
    assert "Thresholds" in block
