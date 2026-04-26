"""Deterministic naming for run identifiers, branches, and worktree directories.

Run IDs use a sortable timestamp prefix so listing runs in a directory is
naturally chronological. Branch and directory names are slug-safe and bounded
in length so they fit common git refspec rules and Windows path limits.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase, hyphen-only slug. Empty input becomes 'feature'."""
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not cleaned:
        cleaned = "feature"
    return cleaned[:max_len].rstrip("-") or "feature"


def new_run_id() -> str:
    """`YYYYMMDDTHHMMSSZ-<6char>` — sortable + unique."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{stamp}-{suffix}"


def branch_name(run_id: str, feature: str | None) -> str:
    slug = slugify(feature or "feature")
    short_run = run_id.split("-")[-1]
    return f"smithic/{slug}-{short_run}"


def worktree_dirname(run_id: str) -> str:
    return f"run-{run_id}"
