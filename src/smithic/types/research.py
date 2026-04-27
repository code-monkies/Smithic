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
    - ``url`` is optional. Real runs show the model often collapses
      citation-pointer + label into a single ``source`` field (e.g.
      ``"source": "github.com/anthropics/claude-code/issues/26171"``) and
      omits ``url`` entirely. When ``source`` looks URL-ish, we promote it
      into ``url`` so the existing title-from-URL fill still works.
    - ``title`` defaults to the URL path stem (or ``source`` text when no
      URL is present) if the model omits it.
    - ``snippet`` accepts ``excerpt``/``summary`` as aliases since those are
      the words the model reaches for naturally; over-length snippets are
      truncated rather than rejected.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    source: str = "web"
    url: str = Field(
        default="",
        validation_alias=AliasChoices("url", "source_url", "link", "href"),
    )
    title: str = ""
    snippet: str = Field(
        default="",
        validation_alias=AliasChoices("snippet", "excerpt", "summary", "description", "body"),
    )
    posted_at: datetime | None = None
    score_signal: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _promote_source_to_url(cls, data: object) -> object:
        """When ``url`` is missing but ``source`` looks URL-ish, copy it over.

        The synth model's natural shape uses ``source`` as the citation
        pointer (URL or domain+path), with a separate ``summary`` for the
        body. Without this promotion every evidence item fails ``url``-
        required validation. URL-ish heuristic: starts with ``http`` or
        contains a ``/`` AND no whitespace (so ``"news.ycombinator.com/..."``
        promotes but ``"theregister.com 2026/03/31 — ..."`` doesn't).
        """
        if not isinstance(data, dict):
            return data
        has_url = any(k in data for k in ("url", "source_url", "link", "href"))
        if has_url:
            return data
        src = data.get("source")
        if not isinstance(src, str):
            return data
        looks_urly = src.startswith(("http://", "https://")) or (
            "/" in src and not any(c.isspace() for c in src)
        )
        if looks_urly:
            data = dict(data)
            data["url"] = src
        return data

    @field_validator("snippet")
    @classmethod
    def _truncate_snippet(cls, v: str) -> str:
        return v if len(v) <= 500 else v[:497] + "..."

    @model_validator(mode="after")
    def _fill_title(self) -> Evidence:
        if self.title.strip():
            return self
        if self.url:
            parsed = urlparse(self.url)
            stem = (parsed.path.rstrip("/").split("/")[-1] or parsed.netloc or self.url)[:80]
            object.__setattr__(self, "title", stem.replace("-", " ").replace("_", " "))
        elif self.source and self.source != "web":
            object.__setattr__(self, "title", self.source[:80])
        return self


class FeatureCandidate(BaseModel):
    """A single proposed feature, distilled from one or more evidence items."""

    # Lenient — same rationale as Evidence. Truncate over-length titles
    # instead of rejecting them so we don't lose 6 good candidates over
    # one that came back at 81 chars.
    #
    # ``evidence`` was previously ``min_length=1`` but the score stage
    # frequently returns just the candidate's title (without re-listing
    # evidence) — the real evidence still lives in the original
    # ResearchFindings object the orchestrator carries. Empty evidence on a
    # ScoredCandidate is fine; full evidence stays on the research-side
    # FeatureCandidate which has min_length enforced via the research
    # synthesis prompt.
    model_config = ConfigDict(extra="ignore")

    title: str
    description: str = ""
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
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

        Real runs against multiple repos under subscription mode showed the
        scoring subagent picks one of several shapes — none of which are the
        one our schema expects. Catalogued so far:

        1. **Flattened candidate object.** Title/description/evidence/
           inferred_user_pain are emitted directly on the scored item:
           ``{"title": "...", "description": "...", "axes": {...}}``.
           We lift those keys into a ``candidate`` sub-object.
        2. **`candidate_title` instead of `candidate`.** The model emits a
           single string field naming the candidate by title, with no other
           candidate fields:
           ``{"candidate_title": "Add /healthz", "axes": {...}}``.
           We synthesize a candidate object from just the title.
        3. **`candidate` as a string.** Same idea but the key name matches
           our schema:
           ``{"candidate": "Add /healthz", "axes": {...}}``.
           Wrap the string in ``{"title": ...}``.
        4. **Dict-keyed axes.** Instead of a list of ``{axis, score,
           rationale}`` objects, the model emits a dict keyed by axis name:
           ``{"market_demand": {"score": 0.8, ...}, ...}``. Re-shape to the
           list form, lifting the dict key into an explicit ``axis`` field.

        Without this normalization, the entire run aborts at score even when
        the model's actual *content* is fine — pure shape mismatch.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)  # don't mutate caller's dict

        # Variant 3: candidate is a bare string — wrap as {"title": ...}.
        candidate_field = data.get("candidate")
        if isinstance(candidate_field, str):
            data["candidate"] = {"title": candidate_field}

        # Variant 1: lift flattened candidate keys into a nested object.
        if "candidate" not in data:
            candidate_keys = ("title", "description", "evidence", "inferred_user_pain")
            lifted = {k: data.pop(k) for k in candidate_keys if k in data}
            if lifted:
                data["candidate"] = lifted

        # Variant 2: candidate_title (or the natural-language synonyms a model
        # might reach for) → synthesize a candidate from the title.
        if "candidate" not in data:
            for title_key in ("candidate_title", "feature_title", "feature", "name"):
                if title_key in data and isinstance(data[title_key], str):
                    data["candidate"] = {"title": data.pop(title_key)}
                    break

        # Variant 4: dict-shaped axes → list with explicit axis field.
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
