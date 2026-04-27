"""Implement-stage tests — focus on the worktree-isolation contract.

The Claude SDK call itself is integration-tested elsewhere (and is too
expensive to fake at this layer). These tests cover the pieces that broke
in real subscription-mode runs:

1. The system prompt MUST embed the worktree path and forbid edits outside
   it. Without this, the agent took spec.md's absolute target-repo path as
   authoritative and wrote files back into the parent repo.
"""

from __future__ import annotations

from pathlib import Path

from smithic.stages.implement import _build_system_prompt


def test_system_prompt_embeds_worktree_path() -> None:
    worktree = Path("/tmp/smithic-worktrees/run-abc")
    prompt = _build_system_prompt(worktree)
    assert str(worktree) in prompt
    assert "Worktree (your sandbox)" in prompt


def test_system_prompt_forbids_absolute_paths_to_parent_repo() -> None:
    """Belt-and-suspenders: the agent is told *explicitly* not to use absolute
    paths to a parent repo even when one is mentioned in the spec."""
    worktree = Path("/tmp/smithic-worktrees/run-abc")
    prompt = _build_system_prompt(worktree)
    assert "Never use an absolute path to a parent repository" in prompt
    assert "must stay" in prompt
