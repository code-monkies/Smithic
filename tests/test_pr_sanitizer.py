"""The PR stage must drop reserved labels even if a buggy caller smuggles them in."""

from __future__ import annotations

from smithic.config import RESERVED_LABELS
from smithic.stages.pr import _sanitize_labels


def test_drops_every_reserved_label() -> None:
    incoming = list(RESERVED_LABELS) + ["good-label", "another-fine-one"]
    cleaned = _sanitize_labels(incoming)
    assert set(cleaned) == {"good-label", "another-fine-one"}


def test_preserves_order_of_safe_labels() -> None:
    cleaned = _sanitize_labels(["a", "auto-deploy", "b", "deploy", "c"])
    assert cleaned == ["a", "b", "c"]
