"""Configuration schema and loader for `smithic.toml`."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Labels Smithic refuses to auto-apply, regardless of config. These commonly
# trigger CI/deploy workflows in the wild and an autonomous run should never
# initiate a deploy.
RESERVED_LABELS: frozenset[str] = frozenset(
    {"dev-tracked", "auto-deploy", "production", "release", "ship-it", "deploy"}
)


class TargetConfig(BaseModel):
    """Where to find the target repo and its mission."""

    model_config = ConfigDict(extra="forbid")

    path: Path
    mission: Path | None = None
    mission_text: str | None = None

    @model_validator(mode="after")
    def _exactly_one_mission(self) -> TargetConfig:
        has_file = self.mission is not None
        has_text = self.mission_text is not None and self.mission_text.strip() != ""
        if has_file == has_text:
            raise ValueError(
                "[target] must set exactly one of `mission` (path) or `mission_text` (inline)"
            )
        return self

    def resolve_mission(self, config_dir: Path) -> str:
        """Return the mission body, reading from disk if `mission` is set."""
        if self.mission_text is not None:
            return self.mission_text.strip()
        assert self.mission is not None
        path = self.mission if self.mission.is_absolute() else (config_dir / self.mission)
        return path.read_text(encoding="utf-8").strip()

    def resolve_path(self, config_dir: Path) -> Path:
        return self.path if self.path.is_absolute() else (config_dir / self.path).resolve()


class SwarmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parallel_runs: int = Field(default=1, ge=1, le=20)
    worktree_root: str = ".smithic-worktrees"


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_usd_per_run: float = Field(default=5.00, gt=0)
    max_tokens_per_run: int = Field(default=2_000_000, gt=0)


class AuthConfig(BaseModel):
    """How the Claude Agent SDK should authenticate.

    `mode` accepts:

    - ``auto`` (default): pick at runtime based on env. Bedrock / Vertex /
      Foundry env vars beat ``ANTHROPIC_API_KEY`` beats subscription session.
    - ``api``: require ``ANTHROPIC_API_KEY``; per-token billing.
    - ``subscription``: use the logged-in ``claude`` CLI session (Pro / Max
      plan). USD ceiling is treated as advisory in this mode because the SDK
      reports cost as $0 for subscription calls.
    - ``bedrock`` / ``vertex`` / ``foundry``: route through the named cloud
      provider; sets ``CLAUDE_CODE_USE_<PROVIDER>=1`` and disables
      Anthropic-specific beta headers.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "api", "subscription", "bedrock", "vertex", "foundry"] = "auto"
    cli_path: str | None = None


class ResearchConfig(BaseModel):
    """Where Smithic looks for market signal in the research stage.

    ``sources`` accepts ``"web"``, ``"reddit"``, ``"hn"``, ``"producthunt"``.
    ``hn`` and ``producthunt`` are reserved for v0.3 — listing them in v0.2 is
    not an error, the unrecognized entries are silently skipped at registry-
    build time.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[str] = Field(default_factory=lambda: ["web", "reddit"])
    cache_ttl_hours: int = Field(default=72, gt=0)
    max_candidates: int = Field(default=5, ge=1, le=8)
    query_budget_usd: float = Field(default=0.10, gt=0)


class RubricConfig(BaseModel):
    """Optional override path for the value-scoring rubric.

    If unset, Smithic uses the bundled default at
    ``src/smithic/rubric/default.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    path: Path | None = None


class CritiqueConfig(BaseModel):
    """How the critic stage behaves."""

    model_config = ConfigDict(extra="forbid")

    enable: bool = True
    max_revise_loops: int = Field(default=1, ge=0, le=3)
    model: str | None = None  # falls back to [auth].model / SDK default


class PRConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_on_critique_concerns: bool = True
    labels: list[str] = Field(default_factory=list)
    base_branch: str = "main"

    @model_validator(mode="after")
    def _no_reserved_labels(self) -> PRConfig:
        clashes = {label for label in self.labels if label in RESERVED_LABELS}
        if clashes:
            raise ValueError(
                f"refusing to auto-apply reserved labels {sorted(clashes)} — "
                "Smithic never auto-applies CI/deploy-trigger labels"
            )
        return self


class SmithicConfig(BaseModel):
    """Top-level `smithic.toml` schema."""

    model_config = ConfigDict(extra="forbid")

    target: TargetConfig
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    rubric: RubricConfig = Field(default_factory=RubricConfig)
    critique: CritiqueConfig = Field(default_factory=CritiqueConfig)
    pr: PRConfig = Field(default_factory=PRConfig)


def load_config(path: Path) -> tuple[SmithicConfig, Path]:
    """Load and validate a `smithic.toml`. Returns (config, config_dir)."""
    config_dir = path.parent.resolve()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return SmithicConfig.model_validate(raw), config_dir
