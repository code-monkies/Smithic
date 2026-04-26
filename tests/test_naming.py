"""Naming-helper tests."""

from __future__ import annotations

from smithic.worktree.naming import branch_name, new_run_id, slugify, worktree_dirname


def test_slugify_strips_punctuation() -> None:
    assert slugify("Add /healthz endpoint!") == "add-healthz-endpoint"


def test_slugify_falls_back_for_empty_input() -> None:
    assert slugify("") == "feature"
    assert slugify("!!!") == "feature"


def test_slugify_respects_max_len() -> None:
    long = "x" * 100
    assert len(slugify(long, max_len=20)) == 20


def test_run_id_format() -> None:
    rid = new_run_id()
    head, suffix = rid.rsplit("-", 1)
    assert head.endswith("Z")
    assert len(suffix) == 6


def test_branch_name_includes_slug_and_run_suffix() -> None:
    rid = "20260101T000000Z-abcdef"
    branch = branch_name(rid, "Add healthz")
    assert branch.startswith("smithic/add-healthz-")
    assert branch.endswith("-abcdef")


def test_worktree_dirname_uses_run_id() -> None:
    rid = "20260101T000000Z-abcdef"
    assert worktree_dirname(rid) == f"run-{rid}"
