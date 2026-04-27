"""Spec generation — turns a feature description and introspection report into a markdown spec.

The spec stage composes a structured spec document from the inputs and writes
it to ``.smithic/spec.md`` inside the worktree. The implementation stage will
read it.

In v0.2 this stage optionally embeds the research/scoring rationale block when
the run was driven by the autonomous-ideation loop (i.e., ``--feature`` was
not supplied). When ``--feature`` is supplied, no rationale is shown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from smithic.stages.introspect import IntrospectionReport


def write_spec(
    *,
    worktree_path: Path,
    feature: str,
    mission: str,
    introspection: IntrospectionReport,
    run_id: str,
    rationale: str | None = None,
) -> Path:
    """Write ``.smithic/spec.md`` inside the worktree and return its path."""
    smithic_dir = worktree_path / ".smithic"
    smithic_dir.mkdir(parents=True, exist_ok=True)
    spec_path = smithic_dir / "spec.md"

    timestamp = datetime.now(UTC).isoformat()

    rationale_block = ""
    if rationale and rationale.strip():
        rationale_block = (
            "\n## Why this feature\n\n"
            "Selected by Smithic's autonomous-ideation loop based on the research\n"
            "brief at `.smithic/research.md`. Rationale:\n\n"
            f"{rationale.strip()}\n"
        )

    body = f"""# Smithic feature spec

- **Run ID**: `{run_id}`
- **Generated**: {timestamp}

## Feature

{feature.strip()}
{rationale_block}
## Mission context

{mission.strip()}

## Worktree

The implementation agent runs with `cwd` set to the worktree below.
**All file edits MUST stay inside this directory** — Smithic isolates each
run in a worktree so multiple parallel runs don't trample each other and so
a failed run never leaks changes back into the parent repo. Use relative
paths (or paths starting with the worktree path); never absolute paths to
the parent repo.

- **Worktree path**: `{worktree_path}`

## Repo briefing

{introspection.as_briefing(display_path=worktree_path)}

## Acceptance criteria

The implementation stage should:

1. Implement the feature described above in a way consistent with the repo's existing
   conventions (see CLAUDE.md if present).
2. Add or update tests appropriate to the project's existing test framework. If no test
   framework is configured, add a minimal smoke check rather than skipping verification.
3. Keep the change as small as possible while still implementing the feature end-to-end.
   Resist the urge to refactor adjacent code.
4. Update relevant documentation (README, CHANGELOG, or inline docstrings) only when the
   feature changes user-visible behavior or public API.
"""
    spec_path.write_text(body, encoding="utf-8")
    return spec_path
