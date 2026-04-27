"""The PR stage must drop reserved labels even if a buggy caller smuggles them in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smithic.config import RESERVED_LABELS
from smithic.stages.pr import (
    NEEDS_REVIEW_LABEL,
    _ensure_smithic_label_exists,
    _label_exists,
    _sanitize_labels,
)


def test_drops_every_reserved_label() -> None:
    incoming = list(RESERVED_LABELS) + ["good-label", "another-fine-one"]
    cleaned = _sanitize_labels(incoming)
    assert set(cleaned) == {"good-label", "another-fine-one"}


def test_preserves_order_of_safe_labels() -> None:
    cleaned = _sanitize_labels(["a", "auto-deploy", "b", "deploy", "c"])
    assert cleaned == ["a", "b", "c"]


class _FakeProc:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_label_exists_returns_true_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh label list`` returns the label — we say True."""
    payload = json.dumps([{"name": "smithic-needs-review"}, {"name": "bug"}])

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        assert cmd[:3] == ["gh", "label", "list"]
        return _FakeProc(returncode=0, stdout=payload)

    monkeypatch.setattr("smithic.stages.pr.subprocess.run", fake_run)
    assert _label_exists("smithic-needs-review", cwd=Path(".")) is True


def test_label_exists_returns_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps([{"name": "bug"}, {"name": "documentation"}])

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        return _FakeProc(returncode=0, stdout=payload)

    monkeypatch.setattr("smithic.stages.pr.subprocess.run", fake_run)
    assert _label_exists("smithic-needs-review", cwd=Path(".")) is False


def test_label_exists_returns_none_on_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If we can't query, return None — caller treats it as soft fail."""
    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        return _FakeProc(returncode=1, stderr="not authenticated")

    monkeypatch.setattr("smithic.stages.pr.subprocess.run", fake_run)
    assert _label_exists("smithic-needs-review", cwd=Path(".")) is None


def test_ensure_label_skips_create_when_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-present label → no `gh label create` call."""
    calls: list[list[str]] = []
    payload = json.dumps([{"name": NEEDS_REVIEW_LABEL}])

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        calls.append(cmd)
        if cmd[:3] == ["gh", "label", "list"]:
            return _FakeProc(returncode=0, stdout=payload)
        return _FakeProc(returncode=0)

    monkeypatch.setattr("smithic.stages.pr.subprocess.run", fake_run)
    _ensure_smithic_label_exists(NEEDS_REVIEW_LABEL, cwd=Path("."))

    creates = [c for c in calls if c[:3] == ["gh", "label", "create"]]
    assert creates == [], "must not create a label that already exists"


def test_ensure_label_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing label → `gh label create` is invoked once with sensible args."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        calls.append(cmd)
        if cmd[:3] == ["gh", "label", "list"]:
            return _FakeProc(returncode=0, stdout=json.dumps([]))
        return _FakeProc(returncode=0)

    monkeypatch.setattr("smithic.stages.pr.subprocess.run", fake_run)
    _ensure_smithic_label_exists(NEEDS_REVIEW_LABEL, cwd=Path("."))

    creates = [c for c in calls if c[:3] == ["gh", "label", "create"]]
    assert len(creates) == 1
    assert creates[0][3] == NEEDS_REVIEW_LABEL
    assert "--description" in creates[0]
    assert "--color" in creates[0]
