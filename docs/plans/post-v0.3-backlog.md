# Post-v0.3 backlog

> **For the implementing agent.** This is a parking-lot document, not a release plan. Each section is a sketch of work that was explicitly out-of-scope for `v0.2` or `v0.3`. The maintainer decides what becomes `v0.4` (and what gets dropped); this doc just makes sure nothing is forgotten.
>
> Read [v0.2-autonomous-ideation.md](v0.2-autonomous-ideation.md) and [v0.3-swarm.md](v0.3-swarm.md) before picking anything up — those plans set the conventions every item below assumes.

## Why a backlog instead of one big plan

The v0.3 hand-off note said: "Smithic v0.3 is the v1.0 candidate — the next iteration is hardening (more MCPs, better rubrics, observability) and judgment about whether to start a v1.0 line. Open a discussion issue rather than auto-planning v0.4."

Items here have not been triaged with that judgment in mind. Each is a sketch with enough detail to:

- Decide whether it's worth doing.
- Rough out its scope before committing to a release.
- Catch dependencies between items (e.g. "vector cache should land before private signals or we'll re-design twice").

When one of these graduates into a release, lift it into its own `vX.Y-<name>.md` plan and follow the same shape as v0.2/v0.3 (Context / Goals / Out of scope / Decisions / File changes / Testing / Hand-off).

## Suggested ordering

A defensible sequence based on dependencies, not committed:

```
                    ┌──── observability ─────┐
v0.3 (shipped) ─────┤                        ├─── private signals ─── distributed swarms
                    └──── vector cache ──────┴─── scheduling
```

- **Observability** first because it makes everything else debuggable.
- **Vector cache** is a small, isolated upgrade with no downstream dependencies.
- **Private signals** depends on observability (for ROI tracking) and benefits from a smarter cache.
- **Scheduling** layers on observability — knowing what a routine produced is the whole point.
- **Distributed swarms** is the v1.0-shaped item; it touches everything.

---

## 1. Observability + demo

**Why deferred from v0.3:** "Web UI" was explicitly out-of-scope. v0.3 ships JSON-line telemetry to stdout and a `smithic status` table — fine for one user, painful when reviewing a swarm of 20 runs.

**Scope sketch:**

- A read-only local web dashboard, served by `smithic dashboard` on `localhost:8788`. Reads the existing SQLite ledger; no daemon required.
- Three views: parent runs (table), child run detail (timeline of stages + cost + the spec/research/score JSON files inline), MCP-server activity (cache hit rate, source skip events).
- Asciinema cast + a 90-second video walkthrough committed under `docs/demo/`. The v0.3 plan called for it; recording is a manual step that a human has to do.
- Aggregate metrics: cost-per-PR, cache hit rate per target, critic-verdict distribution. Surface in the dashboard *and* as `smithic stats --json` for users who'd rather pipe to their own tooling.

**Approach notes:**

- Use `fastapi` + `htmx` over a heavy SPA framework. The data shape is small and the UX is "hit refresh," not "real-time stream." Avoids npm.
- Templates are server-rendered Jinja. Static assets minimal (Pico.css or similar). Total weight under 200KB.
- The dashboard reads from the same SQLite file Smithic writes — no API layer, just queries.

**Risk:** Scope-creep into write operations (replay this run, retry this child, edit the rubric). v0.4 should be **read-only** by design. Write paths belong in a later release once read-only is solid.

**Prereq:** None. Can be built against the v0.3 schema as-is.

**Estimated size:** ~1.2k LOC, mostly templates and queries. ~30 tests.

---

## 2. Vector-similarity research cache

**Why deferred from v0.3:** v0.3 chose SQLite exact-match on the normalized query set hash. Real cache hit rate in a swarm depends on the *first* child's query planner producing the same exact set as siblings — which usually happens for the same target + mission, but not always.

**Scope sketch:**

- Optional extra dep `smithic[cache]` that pulls in `sentence-transformers` (currently the cleanest local-embedding option that runs on Windows) plus `chromadb` (or `lancedb` if Chroma's WAL behavior on Windows continues to be flaky in 2026).
- Lookup logic: if the extra is installed, fall back to vector similarity over query embeddings with `cosine_threshold ≥ 0.85` after the exact-match miss. Both cache backends keyed by the same `(target_hash, ...)` shape so the SQLite store stays the source of truth and the vector index is purely an index.
- `smithic clean --cache --rebuild-index` to nuke and re-embed. Useful when you change the embedding model.

**Approach notes:**

- Don't make the embedding dep a hard requirement. The 200MB ST model is hostile to "I just want to try it" users. Skip-with-log when missing — same pattern v0.3 uses for Product Hunt without a token.
- Embed on `store()`, not on `lookup()`. Lookup must stay fast.
- Keep the SQLite cache as the durable record; the vector index is regenerable. If they get out of sync, drop the index and rebuild.

**Risk:** Embedding model drift between releases. If we upgrade the model, old vectors are useless. Document the model name in the index metadata and force a rebuild on mismatch.

**Prereq:** None. v0.3's cache module has a clean `lookup()` / `store()` boundary that swaps cleanly.

**Estimated size:** ~400 LOC, ~15 tests. Optional dep so the core install stays small.

---

## 3. Private-data signal sources

**Why deferred from v0.2:** "Private-data signal sources (analytics, logs)" was explicit out-of-scope. Public web + community signals were enough for v0.2 — but founders running real products have a much richer signal in their own data: error rates spiking on a feature, support tickets clustering around a missing capability, analytics showing where users drop off.

**Scope sketch:**

- A new `[research.private]` config block listing private sources to consult in addition to public ones. Each source needs a credential the user supplies via env var (Smithic does NOT store API tokens).
- Bundled MCP servers under `src/smithic/mcp/custom/` for the obvious shortlist:
  - `github_issues` — issues + discussions on the user's repo (repo is already declared in target config; just needs a `GITHUB_TOKEN` with `repo` scope).
  - `sentry` — recent error groups + frequency for a `SENTRY_PROJECT` slug.
  - `posthog` / `plausible` — feature-flag adoption + drop-off funnels (one server each, both API-driven).
  - `intercom` / `zendesk` — support ticket search by tag/keyword (one each, optional).
- Evidence schema gets a new `source` literal `"private"` and a `private_source: str` field naming which system it came from. Reviewers need to know they're seeing internal data so they can sanity-check whether to ship it.
- The synthesis prompt explicitly weights private signals: "If a candidate has private-source evidence (real users hitting this), prefer it over public-source candidates with similar rubric scores."

**Approach notes:**

- Treat private data as **higher trust, lower volume**. Three Sentry errors are stronger signal than thirty Reddit threads.
- Never let private-source URLs leak into PR bodies. Public README / search-engine indexing of a Smithic-generated PR is the dominant case; an internal Sentry link in there is a compliance footgun. Strip or redact in `compose_pr_body`.
- The rubric needs a new `private_signal_strength` axis (or weight private evidence inside `market_demand`). Default rubric should ship updated; users can override.

**Risk:** Sensitive data ending up in committed artifacts (`.smithic/research.json` ships in the PR). Redact at the *artifact-write* step, not the synthesis step — that way the model still sees the full evidence but the PR doesn't expose it.

**Prereq:** Observability (you'll want to track which sources actually drove selections). Vector cache is helpful but not blocking.

**Estimated size:** ~300 LOC per source × 4 sources = ~1.2k LOC, ~50 tests, plus the redaction layer.

---

## 4. Background scheduling

**Why deferred from v0.3:** "Background scheduling. No cron, no daemons. The user invokes; the swarm runs; the user reviews. (`smithic` is not a service.)"

That stance is right for v0.3 — but a founder who invokes weekly is a founder who'd rather Smithic invoked itself. The right model isn't a daemon Smithic ships; it's first-class support for the schedulers users already have (cron, GitHub Actions, systemd timers).

**Scope sketch:**

- `smithic run --scheduled` mode: identical to a normal run *except* it skips when (a) a previous run for the same parent target is still in progress, (b) the last run for this target completed less than `[schedule].min_interval_hours` ago, (c) the working tree of the target has uncommitted changes (don't sneak a PR in over your own WIP).
- A `[schedule]` config block with `min_interval_hours`, `max_runs_per_window`, and a `dedup_window_days` that prevents the rubric from re-picking a feature that's already been shipped (or rejected) in the last N days.
- A bundled `examples/github-actions/smithic.yml` that runs `smithic run --scheduled --runs 3` on a weekly cron. Same pattern for `crontab` and `systemd`.
- Telemetry events: `schedule.skipped` with a reason, `schedule.fired`. Surface in the v0.4 dashboard so the user can answer "is my routine actually doing anything."

**Approach notes:**

- The dedup-window is the load-bearing piece. Without it, scheduling produces N PRs for the same idea over N weeks. Implement it as a rubric-side disqualification: read selected_candidate_title from the last `dedup_window_days` of runs against this target, disqualify any candidate whose title or description is too similar (use the same vector-similarity check as the cache, if installed; fall back to title prefix).
- "Uncommitted changes" check is `git status --porcelain` against the target. Cheap and explicit.
- Don't try to be cron. Be the thing cron triggers.

**Risk:** Drift in private-data signals across runs that the user can't see. The dashboard view of "last 30 days of routine runs + what they picked + what's still open" is the antidote.

**Prereq:** Observability (otherwise users can't tell if their routine is healthy). Vector cache helps with the dedup check.

**Estimated size:** ~600 LOC, ~25 tests. Mostly the dedup logic and the skip conditions.

---

## 5. Distributed swarms

**Why deferred from v0.3:** "All runs are on one machine, in one process, against one target. Multi-machine fan-out is a future release."

This is the v1.0-shaped item. Most of the others are extensions; this one is a re-architecture. Earn the right to do it by feeling the pain of single-machine swarms first.

**Scope sketch:**

- A "controller" process that owns the SQLite ledger (or, more likely, a real Postgres) and a job queue.
- "Worker" processes that pull jobs from the queue, run a single child, push the outcome back. Stateless except for the worker's local clone of the target repo.
- The controller is the new entry point: `smithic run --runs 20 --workers remote` registers a parent, queues 20 jobs, returns immediately. `smithic status --watch` polls.
- Workers can run on the same machine (cheap) or on separate machines / containers / GitHub-Actions runners (the unlock). Worker registration via `smithic worker --controller https://controller.local:8788`.
- Backpressure: the queue, not the controller, is the source of truth. A worker dying mid-run means its job times out and gets re-queued (idempotency required — child runs must be safe to retry from scratch).

**Approach notes:**

- Don't write a queue. Use Postgres SKIP LOCKED, Redis Streams, or NATS — pick one and stop. Each works; the choice is "what's the user already running."
- Workers must NOT share git worktrees across machines. Each worker clones the target fresh into its own scratch dir. This is fine because the per-run cost is dominated by Claude calls, not `git clone`.
- The shared research cache becomes the controller's responsibility — workers send queries to it instead of having their own. This is the natural place for a real cache service (Redis, or pgvector in Postgres).

**Risk:** Operational complexity. v0.3 is "install via pipx." v1.0 distributed mode shouldn't *require* that, even if it enables it. Single-machine should still work without changes.

**Prereq:** Vector cache (because the cache needs to be a service, not a SQLite file). Observability (because debugging a 20-worker run with three controllers and no dashboard is a nightmare).

**Estimated size:** ~3.5k LOC, the largest item by far. Likely a 2-3 month effort with one focused implementer.

---

## 6. Cross-run negotiation (research-grade, not roadmap-grade)

**Why deferred from v0.3:** "Cross-run negotiation. Runs do not chat with each other. The 'diversity nudge' is the closest we get; it's a one-way read of what previous runs in the same parent picked, not a coordination protocol."

This is the speculative item. The v0.3 plan was right to keep it out — the diversity nudge handles 90% of the practical "don't all pick the same feature" need. Negotiation buys the remaining 10% at substantial complexity cost.

**Scope sketch (intentionally vague):**

- Open question: is there value in letting a child *propose* a feature and have siblings *vote* before any commit to scoring it? Or in letting a child see another's *partial* implement progress and decide to do something complementary?
- Both shapes risk the production-failure-mode of multi-agent systems the project has been carefully avoiding since v0.1.

**If pursued:** Write it as a research spike. Fork a branch, prototype against the v0.3 swarm, measure whether selected-feature diversity actually improves vs. the baseline. Don't merge unless the data is unambiguous.

**Prereq:** v1.0 distributed swarms (negotiation requires more than one process to coordinate; with one in-process task group, just compute it directly).

---

## 7. Asciinema demo

**Why skipped in v0.3:** Recording is a manual step. The plan listed it under verification-checklist; my v0.3 implementation skipped it explicitly.

**Scope sketch:**

- Record a ~90 second cast: `smithic run --runs 3 --max-usd 2.00`, watch parallel children land 3 PRs.
- Embed it in the README's "Status" section.
- Also: a 30-second `--research-only` cast for the cheap-probe story.

**Risk:** None. This is just sitting down and recording. Bundle into observability.

**Estimated size:** ~0 LOC. Two files in `docs/demo/`.

---

## What's NOT on this list (intentionally)

These were considered and rejected as bad ideas, not deferred:

- **A Smithic SaaS.** The whole point is local-first. If a user wants hosted Smithic, they can run the dashboard somewhere and forward worker registrations. Don't host it ourselves.
- **An LLM-agnostic abstraction layer.** `claude-agent-sdk` is the moat. Re-implementing against OpenAI / Gemini / etc. would 4x the surface area for a smaller benefit than any of the items above.
- **A plugin marketplace.** MCP is already the plugin system. Don't reinvent it. If the registry of community MCP servers needs a UX, push it back into the MCP ecosystem; don't fork.
- **Replay / time-travel debugging.** Cool. Not where the value is. Stage payloads + the SQLite ledger already give you forensic readability after-the-fact.

If any of these get re-pitched, the burden of proof is on showing why the cost-benefit changed.

---

## Hand-off

When picking up an item:

1. Lift it into its own `vX.Y-<slug>.md` file under `docs/plans/`.
2. Expand the sketch into the full plan structure (Context / Goals / Out of scope / Decisions / File changes / Testing / Verification checklist / Suggested commit boundaries / Hand-off).
3. Update `docs/plans/README.md` to add the row.
4. Open a discussion issue *first* if the item involves a re-architecture (items 5 or 6). The single-author-decides flow worked for v0.2/v0.3 because each was a clear extension; v1.0 deserves more deliberation.
