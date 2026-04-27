"""Load + validate rubric YAML, merging a user override over the default.

Merge semantics: the user's YAML is layered on top of the bundled default at
the *axis* level. So a user can replace a single axis's weight or description
without re-stating every axis. To remove a default axis, set its weight to
``0`` in the override and the loader will drop it before validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from smithic.rubric.schema import Axis, Rubric, Thresholds

DEFAULT_RUBRIC_PATH = Path(__file__).parent / "default.yaml"


class RubricError(ValueError):
    """Raised when a rubric file is missing, malformed, or fails validation."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise RubricError(f"rubric file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise RubricError(f"rubric file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RubricError(f"rubric file {path} must be a mapping at the top level")
    return data


def _merge(default: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Layer ``override`` on top of ``default`` at the axis / thresholds level."""
    merged: dict[str, Any] = {
        "axes": dict(default.get("axes") or {}),
        "thresholds": dict(default.get("thresholds") or {}),
    }
    for name, axis in (override.get("axes") or {}).items():
        if not isinstance(axis, dict):
            raise RubricError(f"axis {name!r} must be a mapping")
        existing = merged["axes"].get(name) or {}
        merged["axes"][name] = {**existing, **axis}

    for key, value in (override.get("thresholds") or {}).items():
        merged["thresholds"][key] = value

    # Drop axes whose effective weight is 0 — lets the user remove a default.
    merged["axes"] = {
        name: axis
        for name, axis in merged["axes"].items()
        if axis.get("weight", 0) and axis["weight"] > 0
    }
    return merged


def load_rubric(override_path: Path | None = None) -> Rubric:
    """Return a validated ``Rubric``.

    If ``override_path`` is provided, its contents are merged over the bundled
    default. If not, the default is returned as-is.
    """
    default_raw = _read_yaml(DEFAULT_RUBRIC_PATH)
    if override_path is None:
        raw = default_raw
    else:
        override_raw = _read_yaml(override_path)
        raw = _merge(default_raw, override_raw)

    try:
        axes = {
            name: Axis(**axis) if not isinstance(axis, Axis) else axis
            for name, axis in (raw.get("axes") or {}).items()
        }
        thresholds_raw = raw.get("thresholds") or {}
        thresholds = (
            thresholds_raw
            if isinstance(thresholds_raw, Thresholds)
            else Thresholds(**thresholds_raw)
        )
        return Rubric(axes=axes, thresholds=thresholds)
    except (TypeError, ValueError) as exc:
        raise RubricError(f"rubric validation failed: {exc}") from exc
