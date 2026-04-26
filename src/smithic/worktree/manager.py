"""Cross-platform git worktree lifecycle for run isolation.

Each Smithic run lives in its own ``git worktree`` so multiple runs can iterate
in parallel without stepping on each other and the target repo's main working
tree is never modified. Worktrees are created off a configurable base branch
(default ``main``) and pushed when the run opens its PR.

All paths are ``pathlib.Path`` and all subprocess calls use ``shell=False``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from smithic.worktree.naming import branch_name, worktree_dirname


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base_branch: str


class WorktreeError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


class WorktreeManager:
    """Owns the worktree directory under the target repo."""

    def __init__(self, target_repo: Path, worktree_root: str = ".smithic-worktrees") -> None:
        self.target_repo = target_repo.resolve()
        self.root = (self.target_repo / worktree_root).resolve()

    def create(self, run_id: str, feature: str | None, base_branch: str = "main") -> Worktree:
        """Create a fresh worktree off ``base_branch``."""
        if not (self.target_repo / ".git").exists():
            raise WorktreeError(f"{self.target_repo} is not a git repo")

        self.root.mkdir(parents=True, exist_ok=True)

        branch = branch_name(run_id, feature)
        wt_path = (self.root / worktree_dirname(run_id)).resolve()

        if wt_path.exists():
            raise WorktreeError(f"worktree path already exists: {wt_path}")

        _git(["fetch", "--quiet", "origin", base_branch], cwd=self.target_repo)
        _git(
            ["worktree", "add", "-b", branch, str(wt_path), f"origin/{base_branch}"],
            cwd=self.target_repo,
        )
        return Worktree(path=wt_path, branch=branch, base_branch=base_branch)

    def remove(self, worktree: Worktree, *, force: bool = False) -> None:
        """Remove a worktree and prune the metadata. Does NOT delete the branch."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree.path))
        _git(args, cwd=self.target_repo)

    def list(self) -> list[Path]:
        """Return absolute paths of currently registered worktrees under our root."""
        result = _git(["worktree", "list", "--porcelain"], cwd=self.target_repo)
        paths: list[Path] = []
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                wt = Path(line.removeprefix("worktree ").strip()).resolve()
                try:
                    wt.relative_to(self.root)
                except ValueError:
                    continue
                paths.append(wt)
        return paths
