"""PR creation via the ``gh`` CLI.

Pushes the worktree's branch to ``origin`` and opens a PR with the spec and
implementation summary in the body. Refuses to apply any reserved labels.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from smithic.config import RESERVED_LABELS, PRConfig
from smithic.worktree.manager import Worktree


class PRError(RuntimeError):
    pass


@dataclass(frozen=True)
class PRResult:
    url: str
    branch: str
    is_draft: bool


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # See stages/critique.py::read_diff for why utf-8 + replace is required
    # rather than the platform default.
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise PRError(
            f"{' '.join(cmd)} failed in {cwd}: {stderr or stdout}"
        )
    return result


def _sanitize_labels(labels: list[str]) -> list[str]:
    return [label for label in labels if label not in RESERVED_LABELS]


# Applied automatically when the critic's verdict is ``pass-with-concerns``.
# Not part of ``RESERVED_LABELS`` because Smithic itself adds it; users can
# set up GitHub workflows that watch for this label without it being able to
# trigger a deploy.
NEEDS_REVIEW_LABEL = "smithic-needs-review"
_NEEDS_REVIEW_LABEL_DESCRIPTION = (
    "Smithic-generated PR flagged by the critic for human review"
)
_NEEDS_REVIEW_LABEL_COLOR = "FFA500"  # orange — easy to spot in the PR list


def _label_exists(label: str, *, cwd: Path) -> bool | None:
    """Return True/False if we can determine label existence, else None.

    ``None`` signals "couldn't query" (gh permission issue, network blip,
    unparseable output) — caller treats it as a soft fail and skips the
    create-attempt rather than blowing up the whole PR step on a label-list
    glitch. The subsequent ``gh pr create`` will surface the real error if
    the label is genuinely missing.
    """
    result = subprocess.run(
        ["gh", "label", "list", "--json", "name", "--limit", "200"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        labels = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return any(item.get("name") == label for item in labels if isinstance(item, dict))


def _ensure_smithic_label_exists(label: str, *, cwd: Path) -> None:
    """Idempotently create the Smithic-managed PR-review label if missing.

    Smithic appends ``NEEDS_REVIEW_LABEL`` automatically when the critic
    returns ``pass-with-concerns``. On a fresh repo (or one that hasn't run
    Smithic before — like the very first dogfood run against this repo did),
    that label doesn't exist on GitHub, and ``gh pr create --label <name>``
    fails the entire step with ``could not add label: '<name>' not found``.

    Only Smithic-introduced labels are auto-created here. User-defined
    labels in ``pr_config.labels`` are NOT — a missing one there is a
    misconfiguration the user should fix, not silently paper over.
    """
    if _label_exists(label, cwd=cwd) is True:
        return
    # Best-effort create. If we can't (permissions, race with another
    # creator), the subsequent ``gh pr create`` will surface the real error.
    subprocess.run(
        [
            "gh", "label", "create", label,
            "--description", _NEEDS_REVIEW_LABEL_DESCRIPTION,
            "--color", _NEEDS_REVIEW_LABEL_COLOR,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )


def open_pr(
    *,
    worktree: Worktree,
    title: str,
    body: str,
    pr_config: PRConfig,
    draft: bool,
    extra_labels: list[str] | None = None,
) -> PRResult:
    """Push the worktree branch and open a PR. Returns the PR URL.

    ``extra_labels`` (e.g. ``[NEEDS_REVIEW_LABEL]`` when the critic flagged
    concerns) are appended to ``pr_config.labels`` after sanitization. They
    are still filtered against ``RESERVED_LABELS``.
    """
    if shutil.which("gh") is None:
        raise PRError("gh CLI not found on PATH")

    # Push the branch first. We use --set-upstream so subsequent pushes work.
    _run(
        ["git", "push", "--set-upstream", "origin", worktree.branch],
        cwd=worktree.path,
    )

    cmd: list[str] = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--base",
        pr_config.base_branch,
        "--head",
        worktree.branch,
    ]
    if draft:
        cmd.append("--draft")
    label_set: list[str] = []
    for label in _sanitize_labels([*pr_config.labels, *(extra_labels or [])]):
        if label not in label_set:
            label_set.append(label)
    if NEEDS_REVIEW_LABEL in label_set:
        _ensure_smithic_label_exists(NEEDS_REVIEW_LABEL, cwd=worktree.path)
    for label in label_set:
        cmd.extend(["--label", label])

    result = _run(cmd, cwd=worktree.path)
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not url.startswith("http"):
        raise PRError(f"could not parse PR URL from gh output: {result.stdout!r}")

    return PRResult(url=url, branch=worktree.branch, is_draft=draft)


def compose_pr_body(
    *,
    feature: str,
    mission_excerpt: str,
    impl_summary: str,
    cost_usd: float,
    run_id: str,
    critic_summary: str | None = None,
    rationale: str | None = None,
) -> str:
    sections = [
        "## Feature",
        "",
        feature.strip(),
        "",
    ]
    if rationale and rationale.strip():
        sections.extend(
            [
                "## Why this feature",
                "",
                "Selected by Smithic's autonomous-ideation loop. See "
                "`.smithic/research.md` and `.smithic/score.json` for the full audit trail.",
                "",
                rationale.strip(),
                "",
            ]
        )
    sections.extend(
        [
            "## Mission context",
            "",
            mission_excerpt.strip(),
            "",
            "## Implementation summary",
            "",
            impl_summary.strip(),
            "",
        ]
    )
    if critic_summary and critic_summary.strip():
        sections.extend(
            [
                "## Critic review",
                "",
                critic_summary.strip(),
                "",
            ]
        )
    sections.extend(
        [
            "---",
            "",
            f"<sub>Generated by [Smithic](https://github.com/code-monkies/Smithic) · "
            f"run `{run_id}` · ${cost_usd:.4f} spent</sub>",
            "",
        ]
    )
    return "\n".join(sections)
