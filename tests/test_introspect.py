"""Repo-introspection smoke tests."""

from __future__ import annotations

from pathlib import Path

from smithic.stages.introspect import introspect

FIXTURE = Path(__file__).parent / "fixtures" / "mock_repo"


def test_detects_python_and_pyproject() -> None:
    report = introspect(FIXTURE)
    assert "python" in report.languages_detected
    assert "pyproject.toml" in report.manifests


def test_picks_up_root_claude_md() -> None:
    report = introspect(FIXTURE)
    assert report.has_claude_md
    assert "mock_repo" in report.claude_md_excerpt


def test_suggests_pytest_when_pyproject_mentions_it() -> None:
    report = introspect(FIXTURE)
    assert report.suggested_test_command == "pytest"


def test_briefing_renders_without_error() -> None:
    report = introspect(FIXTURE)
    briefing = report.as_briefing()
    assert "Repo introspection briefing" in briefing
    assert str(FIXTURE.resolve()) in briefing


def test_briefing_display_path_overrides_repo_path() -> None:
    """The briefing must show the *worktree* path when one is supplied.

    Without this, the implement agent reads the absolute target-repo path
    out of spec.md and uses it for Edit/Write — silently bypassing the
    worktree sandbox. See PR #2's worktree-escape fix.
    """
    report = introspect(FIXTURE)
    worktree = Path("/tmp/smithic-worktrees/run-abcd")
    briefing = report.as_briefing(display_path=worktree)
    assert str(worktree) in briefing
    assert str(FIXTURE.resolve()) not in briefing
