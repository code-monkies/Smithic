"""Research, candidate, and scoring value types.

Every cross-stage payload in the research → score → spec pipeline is one of
these models. Stages serialize them to JSON for persistence (worktree
``.smithic/`` files, the SQLite ledger) and reload them via
``model_validate_json`` on the next stage's entry.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class Evidence(BaseModel):
    """One piece of market signal pulled from a research source.

    Lenient on inputs because the synthesis subagent's structured-output
    enforcement is best-effort under subscription auth + MCP tool use. We
    accept whatever shape the model emits and normalize:

    - ``source`` is freeform text — the original ``Literal`` rejected real
      model outputs like ``"Hacker News (Show HN)"`` or domain names.
    - ``title`` defaults to the URL path stem if the model omits it.
    - ``snippet`` accepts ``excerpt``/``summary`` as aliases since those are
      the words the model reaches for naturally; over-length snippets are
      truncated rather than rejected.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: str = "web"
    url: str = Field(validation_alias=AliasChoices("url", "source_url", "link", "href"))
    title: str = ""
    snippet: str = Field(
        default="",
        validation_alias=AliasChoices("snippet", "excerpt", "summary", "description", "body"),
    )
    posted_at: datetime | None = None
    score_signal: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("snippet")
    @classmethod
    def _truncate_snippet(cls, v: str) -> str:
        return v if len(v) <= 500 else v[:497] + "..."

    @model_validator(mode="after")
    def _fill_title_from_url(self) -> Evidence:
        if not self.title.strip() and self.url:
            parsed = urlparse(self.url)
            stem = (parsed.path.rstrip("/").split("/")[-1] or parsed.netloc or self.url)[:80]
            object.__setattr__(self, "title", stem.replace("-", " ").replace("_", " "))
        return self


class FeatureCandidate(BaseModel):
    """A single proposed feature, distilled from one or more evidence items."""

    # Lenient — same rationale as Evidence. Truncate over-length titles
    # instead of rejecting them so we don't lose 6 good candidates over
    # one that came back at 81 chars.
    model_config = ConfigDict(extra="ignore")

    title: str
    description: str = ""
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    inferred_user_pain: str = ""

    @field_validator("title")
    @classmethod
    def _strip_and_truncate_title(cls, v: str) -> str:
        v = v.strip()
        return v if len(v) <= 80 else v[:77] + "..."


class ResearchFindings(BaseModel):
    """The research stage's complete output — fed into the scorer."""

    # Lenient on top-level extras so an extra ``notes`` field from the model
    # doesn't blow the whole parse when the candidates themselves are valid.
    model_config = ConfigDict(extra="ignore")

    candidates: list[FeatureCandidate] = Field(min_length=1, max_length=8)
    queries_run: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)


class AxisScore(BaseModel):
    """One rubric axis evaluated against one candidate."""

    model_config = ConfigDict(extra="ignore")

    axis: str
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class ScoredCandidate(BaseModel):
    """A candidate with its full per-axis scoring breakdown."""

    model_config = ConfigDict(extra="ignore")

    candidate: FeatureCandidate
    axes: list[AxisScore]
    total: float = Field(default=0.0, ge=0.0, le=1.0)
    disqualified: bool = False
    disqualification_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_shape(cls, data: object) -> object:
        """Reshape what the model naturally emits into our schema.

        Real subscription-mode runs against OnlyVAT showed the scoring
        subagent emits two predictable variations the strict schema rejects:

        1. The candidate fields are *flattened* onto the scored item
           (``{"title": "...", "description": "...", "axes": {...}}``)
           instead of nested under ``candidate``. We lift them back.
        2. ``axes`` is a *dict* keyed by axis name
           (``{"market_demand": {"score": 0.8, "rationale": "..."}, ...}``)
           instead of a list of ``{axis, score, rationale}`` objects. We
           re-shape into the list form, taking the dict key as ``axis``.

        Without this, the entire run aborts at score even when the model
        produced perfectly fine values — pure shape mismatch.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)  # don't mutate caller's dict

        # Lift flattened candidate fields back into a nested object.
        if "candidate" not in data:
            candidate_keys = ("title", "description", "evidence", "inferred_user_pain")
            lifted = {k: data.pop(k) for k in candidate_keys if k in data}
            if lifted:
                data["candidate"] = lifted

        # Convert dict-shaped axes back to a list with explicit `axis` field.
        axes = data.get("axes")
        if isinstance(axes, dict):
            data["axes"] = [
                {"axis": name, **(payload if isinstance(payload, dict) else {"score": payload})}
                for name, payload in axes.items()
            ]
        return data


class ScoringResult(BaseModel):
    """The scorer's verdict — either a selected candidate or an abort reason."""

    model_config = ConfigDict(extra="ignore")

    scored: list[ScoredCandidate]
    selected: ScoredCandidate | None = None
    abort_reason: str | None = None
