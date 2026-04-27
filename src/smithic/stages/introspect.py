"""Introspect a target repo to figure out what kind of project it is.

Strategy: prefer authoritative sources first.

1. ``CLAUDE.md`` at the repo root (and ``frontend/CLAUDE.md`` / ``backend/CLAUDE.md``
   if present) — the project author has already written down what's true.
2. Fall back to heuristic stack detection by looking for canonical manifest
   files (``package.json``, ``pyproject.toml``, ``Cargo.toml``, ``go.mod``,
   ``Gemfile``, ``pom.xml``, ``build.gradle``, etc.).
3. Detect a likely test command if a known framework is configured.

The output is a Pydantic model the implementation stage hands to Claude as
context. We deliberately keep this introspection cheap and read-only — the
agent itself is better at deep code analysis once it has a starting frame.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

# (manifest filename, language label) — order matters; first match wins.
_MANIFESTS: list[tuple[str, str]] = [
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("setup.py", "python"),
    ("package.json", "javascript"),
    ("deno.json", "deno"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("Gemfile", "ruby"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "kotlin"),
    ("composer.json", "php"),
    ("mix.exs", "elixir"),
    ("Project.toml", "julia"),
    ("Package.swift", "swift"),
]

# Common test-runner hints keyed by manifest content fragments.
_TEST_HINTS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("pytest", "pytest"),
        ("unittest", "python -m unittest"),
    ],
    "javascript": [
        ("vitest", "npx vitest run"),
        ("jest", "npx jest"),
        ("playwright", "npx playwright test"),
        ("mocha", "npx mocha"),
    ],
    "rust": [("", "cargo test")],
    "go": [("", "go test ./...")],
}


class IntrospectionReport(BaseModel):
    """What Smithic learned about the target repo before implementation."""

    repo_path: Path
    has_claude_md: bool = False
    claude_md_excerpt: str = ""
    nested_claude_mds: list[str] = Field(default_factory=list)
    languages_detected: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    suggested_test_command: str | None = None
    git_default_branch: str | None = None

    def as_briefing(self) -> str:
        """Format the report as a markdown briefing the impl agent can read."""
        lines: list[str] = ["# Repo introspection briefing", ""]
        lines.append(f"- **Path**: `{self.repo_path}`")
        if self.git_default_branch:
            lines.append(f"- **Default branch**: `{self.git_default_branch}`")
        if self.languages_detected:
            lines.append(f"- **Languages detected**: {', '.join(self.languages_detected)}")
        if self.manifests:
            lines.append(f"- **Manifests**: {', '.join(self.manifests)}")
        if self.suggested_test_command:
            lines.append(f"- **Suggested test command**: `{self.suggested_test_command}`")
        if self.has_claude_md:
            lines.append("")
            lines.append("## CLAUDE.md (root) — excerpt")
            lines.append("")
            lines.append(self.claude_md_excerpt)
        if self.nested_claude_mds:
            lines.append("")
            lines.append("## Other CLAUDE.md files present")
            for path in self.nested_claude_mds:
                lines.append(f"- `{path}`")
        return "\n".join(lines)


def _read_excerpt(path: Path, *, max_chars: int = 4000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= max_chars else text[:max_chars] + "\n\n[... truncated ...]"


def _detect_test_command(language: str, manifest_text: str) -> str | None:
    for fragment, command in _TEST_HINTS.get(language, []):
        if fragment == "" or fragment in manifest_text:
            return command
    return None


def introspect(repo_path: Path) -> IntrospectionReport:
    """Cheap, read-only scan of a repo's top-level structure."""
    repo_path = repo_path.resolve()
    report = IntrospectionReport(repo_path=repo_path)

    root_claude = repo_path / "CLAUDE.md"
    if root_claude.is_file():
        report.has_claude_md = True
        report.claude_md_excerpt = _read_excerpt(root_claude)

    for nested in sorted(repo_path.glob("*/CLAUDE.md")):
        rel = nested.relative_to(repo_path).as_posix()
        report.nested_claude_mds.append(rel)

    for manifest, lang in _MANIFESTS:
        m_path = repo_path / manifest
        if not m_path.is_file():
            continue
        report.manifests.append(manifest)
        if lang not in report.languages_detected:
            report.languages_detected.append(lang)
        if report.suggested_test_command is None:
            try:
                content = m_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            report.suggested_test_command = _detect_test_command(lang, content)

    report.git_default_branch = _detect_default_branch(repo_path)

    return report


def _detect_default_branch(repo_path: Path) -> str | None:
    """Resolve the *remote's* default branch, not whatever's currently checked out.

    Reading ``.git/HEAD`` returns the working-tree head — fine when the user
    sits on ``main``, wrong as soon as they're on a feature branch (Smithic
    would then try to fetch + worktree-add off ``feat/foo`` and the run would
    crash on ``couldn't find remote ref feat/foo``).

    Preferred source: ``.git/refs/remotes/origin/HEAD`` which is a symbolic
    ref pointing to the remote's actual default branch. Falls back to packed
    refs, then to ``.git/HEAD`` only when the remote default isn't tracked
    locally (e.g. fresh clones with ``--single-branch``).
    """
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return None

    remote_head = git_dir / "refs" / "remotes" / "origin" / "HEAD"
    if remote_head.is_file():
        text = remote_head.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("ref: refs/remotes/origin/"):
            return text.removeprefix("ref: refs/remotes/origin/")

    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("# ref: refs/remotes/origin/"):
                return line.removeprefix("# ref: refs/remotes/origin/")

    head_file = git_dir / "HEAD"
    if head_file.is_file():
        text = head_file.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("ref: refs/heads/"):
            return text.removeprefix("ref: refs/heads/")

    return None
