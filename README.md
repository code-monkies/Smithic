# Smithic

> An autonomous feature-factory swarm. Point it at a repo + a mission. It opens PRs.

Smithic spawns agent runs that each:

1. **Introspect** the target repo — stack, conventions, what's already built
2. **Research** the market (web + community signals)*
3. **Score** candidate features against a configurable rubric*
4. **Spec** the highest-value next feature
5. **Implement** it inside an isolated git worktree
6. **Critique** its own work*
7. **Open a PR**

Designed to run **N parallel runs** so a founder wakes up to a stack of candidate PRs to triage.

\* All seven steps ship in `v0.2`. Pass `--feature` to skip steps 2–3 and 6 (the autonomous half) for the v0.1-style operator-driven mode.

## Status

`v0.3` — swarm. Pass `--runs N` and Smithic spawns N parallel children sharing one research cache. Each opens its own PR; one child failing doesn't kill siblings. The diversity-nudge keeps siblings from all picking the same feature. See [the plan](#roadmap).

End-to-end validated against a real production repo with subscription auth — every stage (research → score → spec → implement → critique → PR) lands real artifacts and a real PR. See [docs/plans/post-v0.3-backlog.md](docs/plans/post-v0.3-backlog.md) for what's deferred.

## Why

Existing autonomous coding agents (Devin, Factory, MGX, Claude Code itself) execute features a human specifies. Almost nothing closes the loop one step earlier — autonomously *proposing* the right next feature based on real market signal. That gap is what Smithic targets.

## Install

Requires Python 3.12+, [the `gh` CLI](https://cli.github.com/) authenticated against the host where your target repo lives, and git ≥ 2.5 for worktree support.

```bash
pipx install smithic
```

Or, from source:

```bash
git clone https://github.com/code-monkies/Smithic.git
cd Smithic
pip install -e .[dev]
```

### Claude authentication

Smithic delegates the actual coding work to the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), which means you have four ways to authenticate — pick whichever you already have:

| Mode | When to pick | Setup |
|---|---|---|
| `subscription` | You have a **Claude Pro or Max** plan and want to use it. | Run `claude` once, complete `/login`. Smithic reuses that session. |
| `api` | You have an **Anthropic API key** and want per-token billing on the Console. | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `bedrock` | You're routing Claude through **AWS Bedrock**. | AWS credentials set up; Smithic sets `CLAUDE_CODE_USE_BEDROCK=1`. |
| `vertex` | You're routing through **Google Vertex AI**. | GCP creds; Smithic sets `CLAUDE_CODE_USE_VERTEX=1`. |
| `foundry` | You're routing through **Azure AI Foundry**. | Azure creds; Smithic sets `CLAUDE_CODE_USE_FOUNDRY=1`. |

Configure in `smithic.toml`:

```toml
[auth]
mode = "subscription"   # or "api", "bedrock", "vertex", "foundry", or "auto" (default)
# cli_path = "/usr/local/bin/claude"   # optional override
```

Or pass it once at the CLI: `smithic run --auth-mode subscription ...`

**Heads up on subscription mode:** the SDK reports per-call cost as `$0` for subscription usage, so the `max_usd_per_run` budget ceiling becomes advisory — the **token ceiling is still enforced** as the actual hard limit. If `ANTHROPIC_API_KEY` is set in your environment while `mode = "subscription"`, Smithic clears it for SDK calls so subscription billing wins (Claude Code would otherwise prefer the API key).

## Quick start

1. In your **target repo** (the one you want Smithic to ship features into), create a `smithic.toml`:

   ```toml
   [target]
   path = "."
   mission = "./MISSION.md"

   [budget]
   max_usd_per_run = 5.00
   max_tokens_per_run = 2_000_000

   [pr]
   draft_on_critique_concerns = true
   ```

2. Write a one-page `MISSION.md` describing what the project is for, who it's for, and what success looks like. Smithic reads this to ground its proposals.

3. Run:

   ```bash
   # autonomous: Smithic picks the next feature from market research
   smithic run --config ./smithic.toml

   # operator-driven: you specify the feature, skip research+score
   smithic run --config ./smithic.toml --feature "add a /healthz endpoint"

   # cheap probe: write a research brief to .smithic/ and stop
   smithic run --config ./smithic.toml --research-only --max-usd 0.50

   # swarm: 5 parallel children, each picks a different top feature
   smithic run --config ./smithic.toml --runs 5 --max-usd 2.00
   ```

4. Smithic creates a git worktree, spawns a Claude session inside it, lets it implement and test the feature, then opens a PR via `gh`. Watch your repo's PR list.

## When to use parallel runs

Single runs (the default) are right when you have a clear next feature in mind or want to keep cost predictable. The swarm path (`--runs N`) earns its keep when you'd rather wake up to a triage queue than a single proposal — three to five children typically produce two or three PRs worth shipping plus a couple of duds, which is far higher signal than picking one feature in isolation.

Cost notes:

- The first child seeds a shared research cache; children 2..N reuse the synthesized findings when their generated query sets match. Realistic savings are 30–60% of total research spend for swarms of 3+.
- Reddit/HN rate limits start mattering past ~5 concurrent fetches against the same target. Keep `--runs ≤ 5` unless you've supplied a Tavily key.
- The diversity nudge is a soft preference: if all candidates that pass the rubric are the same, siblings will still all converge. That's a signal the rubric is too narrow, not a bug.

## How feature selection works

When `--feature` is omitted Smithic runs an autonomous-ideation loop before it touches code:

1. **Research.** A small Claude subagent derives 3–5 search queries from the mission + introspection. Those queries fan out across the configured sources (Tavily web search if `TAVILY_API_KEY` is set, else `mcp-server-fetch`, plus the bundled Reddit MCP). A second Claude subagent synthesizes the raw evidence into 3–8 `FeatureCandidate` entries with deduplicated supporting links.
2. **Score.** A third Claude session, given a Pydantic schema for structured output, scores each candidate against the rubric in [`src/smithic/rubric/default.yaml`](src/smithic/rubric/default.yaml) (or your override path). Smithic re-computes the weighted total server-side — it doesn't trust the model's arithmetic — and applies the rubric thresholds. Any candidate scoring below `min_per_axis` on any single axis is disqualified, and runs where no candidate clears `min_total` abort cleanly.
3. **Spec + implement** as before, with the chosen candidate's title and rationale embedded in `.smithic/spec.md`.
4. **Critique.** A *fresh* Claude session — no shared context with the implementer — reads the spec and the diff (`git diff <base>...HEAD`) and returns one of `pass`, `pass-with-concerns`, `revise`, `abort`. `pass-with-concerns` opens the PR as a draft with the `smithic-needs-review` label. `revise` hands feedback back to the implement stage for a single retry. `abort` ends the run with no PR (the worktree is preserved for inspection).
5. **PR.** The body includes the research rationale (when autonomous), the implementation summary, and the critic's verdict. The full audit trail — `research.md`, `research.json`, `score.json`, `spec.md` — ships in the diff under `.smithic/` so reviewers can see what evidence drove the proposal.

Override the rubric per-run with `--rubric ./my-rubric.yaml`; disable the critic with `--no-critique` (debugging only — disables the safety net).

## When something goes wrong

Smithic is built around the assumption that any given run can fail in interesting ways — the SDK CLI can crash, the model can return JSON that doesn't match the schema you asked for, the worktree can land in a half-committed state. Every stage drops an inspectable artifact next to the others under `.smithic/` (target dir for research-only, worktree dir otherwise) so you can answer the "what did it actually do?" question after the fact:

| Artifact | When it's written | What's in it |
|---|---|---|
| `research.md` / `research.json` | end of research stage | The full synthesized findings — every candidate with its evidence URLs |
| `score.json` | end of score stage | All candidates with rubric breakdowns, the selected pick, abort reason if any |
| `spec.md` | end of spec stage | What the implement agent was actually told to build |
| `synth-debug-<run_id>.txt` | when research synthesis fails to parse | Raw model output, both the last-message text and the full concatenation |
| `critique-debug.txt` | when the critic returns unparseable output | Structured-output payload + last-message text + full text |
| `critique-stderr.txt` | when the critique CLI subprocess crashes | Captured stderr from the SDK call |
| `implement-stderr.txt` | when the implement CLI subprocess crashes | Captured stderr from the SDK call |
| `smithic.db` (SQLite) | every run | The full ledger — runs, stages, cost events, parent-child relationships. `smithic status` reads from this |

`smithic status --config ./smithic.toml` shows the recent runs table including parent run, status, branch, PR URL, selected candidate, and critic verdict. The DB is the source of truth — if `status` says a run failed, it actually failed.

A few real-world flavor notes from running Smithic against a production repo on Windows + Claude subscription auth:

- **Subscription mode reports cost too.** Docs and source comments say cost is `$0` for subscription, but in practice the SDK does report a non-zero `total_cost_usd` on every call. The token ceiling stays the hard limit; treat the USD figure as informational rather than load-bearing in subscription mode.
- **Windows codecs.** Every Smithic-spawned subprocess (`git`, `gh`) and the env we hand to the SDK forces UTF-8 — without this, real diff content with emojis or smart quotes crashes the cp1252-default text reader. If you see `'charmap' codec` errors anywhere in a stack trace, that's the signal it's an environment Smithic doesn't have utf-8 plumbed into yet.
- **Structured output isn't always in a `TextBlock`.** When `output_format=json_schema` is set, the SDK puts the response on `ResultMessage.structured_output` and may emit zero TextBlocks. Smithic's parsers check structured_output first, last-message text second, full concatenation third — if you're writing a custom stage, do the same.

## Configuration

Full `smithic.toml` schema:

```toml
[target]
path = "/abs/or/relative/path/to/repo"
mission = "./MISSION.md"          # path to file, OR use `mission_text = "..."` inline

[swarm]
parallel_runs = 1                 # v0.3+ honors > 1
worktree_root = ".smithic-worktrees"

[budget]
max_usd_per_run = 5.00
max_tokens_per_run = 2_000_000

[research]                        # v0.2+
sources = ["web", "reddit", "hn"] # "producthunt" available with PRODUCTHUNT_TOKEN
cache_ttl_hours = 72              # how long synthesized findings stay valid in the swarm cache
max_candidates = 5
query_budget_usd = 0.10           # cap on the query-generation Claude call

[rubric]                          # v0.2+
path = ".smithic/rubric.yaml"     # optional override of the bundled default

[critique]                        # v0.2+
enable = true
max_revise_loops = 1
# model = "claude-sonnet-4-6"     # optional stronger model for the safety net

[pr]
draft_on_critique_concerns = true
labels = []                       # never auto-applies CI/deploy-trigger labels
```

CLI flags override config values. Run `smithic --help` for the full list.

## Safety rails

Smithic is built around the assumption that multi-agent systems fail constantly (documented production failure rates of 41–86.7% in 2026, with ~79% from coordination/spec rather than model capability). Concrete mitigations baked in from `v0.1`:

- **Linear orchestrator.** No cyclic agent graphs. Each stage's output is the next stage's input, with Pydantic-typed contracts between them.
- **Worktree isolation.** Every run lives in a `git worktree`; the target repo's main working tree is never touched.
- **Hard cost ceiling.** Token + dollar budgets enforced via the Claude Agent SDK's `max_budget_usd` plus an out-of-band SQLite cost ledger. Breach → run aborts and opens a draft PR labeled `[budget-exceeded]` with whatever it has.
- **Label blocklist.** The framework refuses to auto-apply labels named `dev-tracked`, `auto-deploy`, `production`, `release` etc. PR labeling is an explicit human action.
- **PR-as-checkpoint.** Even on partial failure, every run produces an inspectable artifact.

## Roadmap

- **v0.1 — Plumbing-first** (shipped). End-to-end pipeline: introspect → spec → implement → PR. Feature description supplied via `--feature` flag. Single run.
- **v0.2 — Autonomous ideation** (shipped). Market research stage (web + Reddit MCP), value-scoring rubric, self-critique with abort threshold, tightened budget meter. → [plan](docs/plans/v0.2-autonomous-ideation.md)
- **v0.3 — Swarm** (shipped, this release). `--runs N` parallel children, shared research cache, HN MCP, Product Hunt MCP, diversity-nudge in scoring, parent-aware status table. → [plan](docs/plans/v0.3-swarm.md)
- **Post-v0.3** — observability dashboard, vector-similarity research cache, private signal sources (GitHub issues / Sentry / analytics), background scheduling, distributed swarms. → [backlog](docs/plans/post-v0.3-backlog.md)

Detailed release plans live in [`docs/plans/`](docs/plans/) — each is self-contained so a fresh contributor (human or agent) can pick it up cold.

## Dogfooding — Smithic on Smithic

The repo ships with its own [`smithic.toml`](smithic.toml) + [`MISSION.md`](MISSION.md). Run Smithic against itself to propose its next feature:

```bash
# from the Smithic repo root, with your `claude` CLI logged in:
smithic run --config ./smithic.toml --research-only --max-usd 0.50
# inspect .smithic/research-<run_id>.md
# then a full run if the brief looks reasonable:
smithic run --config ./smithic.toml --max-usd 5.00
```

Same caveats as any target repo: the run will create a worktree at `.smithic-worktrees/`, push a branch, and open a real PR against `code-monkies/Smithic` if `gh` is authed. Review before merging.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The most valuable contributions right now are: new research-source MCP servers under `src/smithic/mcp/custom/`, alternative rubrics in `src/smithic/rubric/`, and stack-detection improvements in `src/smithic/stages/introspect.py`.

## License

[Apache 2.0](LICENSE).
