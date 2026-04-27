"""Spec-stage tests — ensure the worktree path lands in spec.md.

Real subscription-mode runs surfaced an isolation bug where the implement
agent took the briefing's absolute target-repo path as authoritative and
wrote files back into the parent repo, bypassing the worktree. spec.md
must:

1. Show the worktree path in a dedicated "Worktree" section.
2. Pass the worktree path through to the introspection briefing's display
   path so the briefing's "Path:" line points at the sandbox, not the
   parent.
3. Never embed the parent repo's absolute path.
"""

from __future__ import annotations

from pathlib import Path

from smithic.stages.introspect import introspect
from smithic.stages.spec import write_spec

FIXTURE = Path(__file__).parent / "fixtures" / "mock_repo"


def test_spec_shows_worktree_path_not_target_path(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-run-abc"
    worktree.mkdir()
    report = introspect(FIXTURE)

    spec_path = write_spec(
        worktree_path=worktree,
        feature="add a /healthz endpoint",
        mission="ship reliable APIs",
        introspection=report,
        run_id="20260427T000000Z-abc",
        rationale=None,
    )
    body = spec_path.read_text(encoding="utf-8")

    assert "## Worktree" in body
    assert str(worktree) in body
    # The target repo's absolute path must NOT leak into the spec — that's
    # the directive an over-eager agent would follow with absolute Edit calls.
    assert str(FIXTURE.resolve()) not in body
    assert "All file edits MUST stay inside this directory" in body


def test_spec_includes_rationale_when_provided(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-run-abc"
    worktree.mkdir()
    report = introspect(FIXTURE)
    rationale = "**Title:** add a /healthz endpoint\n**Total score:** 0.82"

    spec_path = write_spec(
        worktree_path=worktree,
        feature="add a /healthz endpoint",
        mission="ship reliable APIs",
        introspection=report,
        run_id="20260427T000000Z-abc",
        rationale=rationale,
    )
    body = spec_path.read_text(encoding="utf-8")

    assert "## Why this feature" in body
    assert "Total score" in body
