"""Spec generation — turns a feature description and introspection report into a markdown spec.

In v0.1 the spec stage is intentionally light: it composes a structured spec
document from the inputs and writes it to ``.smithic/spec.md`` inside the
worktree. The implementation stage will read it.

In v0.2+ this stage will also accept a value-scoring rationale from the
research+score stages.
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
) -> Path:
    """Write ``.smithic/spec.md`` inside the worktree and return its path."""
    smithic_dir = worktree_path / ".smithic"
    smithic_dir.mkdir(parents=True, exist_ok=True)
    spec_path = smithic_dir / "spec.md"

    timestamp = datetime.now(UTC).isoformat()
    body = f"""# Smithic feature spec

- **Run ID**: `{run_id}`
- **Generated**: {timestamp}

## Feature

{feature.strip()}

## Mission context

{mission.strip()}

## Repo briefing

{introspection.as_briefing()}

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
