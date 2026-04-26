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

\* `v0.2+`. The `v0.1` plumbing-first release ships steps 1, 4, 5, 7 and accepts the feature description via a CLI flag so you can prove the pipeline end-to-end before the autonomous-ideation loop lands.

## Status

`v0.1` — plumbing-first. Single-run, no parallelism, no autonomous research yet. See [the plan](#roadmap).

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
   # v0.1 — feature description supplied by you
   smithic run --config ./smithic.toml --feature "add a /healthz endpoint"

   # v0.2+ — Smithic picks the feature from market research
   smithic run --config ./smithic.toml
   ```

4. Smithic creates a git worktree, spawns a Claude session inside it, lets it implement and test the feature, then opens a PR via `gh`. Watch your repo's PR list.

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
sources = ["web", "reddit", "hn", "producthunt"]
cache_ttl_hours = 72

[rubric]                          # v0.2+
path = ".smithic/rubric.yaml"

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

- **v0.1 — Plumbing-first** (this release). End-to-end pipeline: introspect → spec → implement → PR. Feature description supplied via `--feature` flag. Single run.
- **v0.2 — Autonomous ideation.** Market research stage (web + Reddit MCP), value-scoring rubric, self-critique with abort threshold, full budget meter.
- **v0.3 — Swarm.** Parallel runs via worktrees, cross-run research cache, full MCP set (HN + Product Hunt), polished examples.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The most valuable v0.1-era contributions are: stack-detection improvements in `src/smithic/stages/introspect.py`, custom MCP servers under `src/smithic/mcp/custom/`, and rubric proposals in `src/smithic/rubric/`.

## License

[Apache 2.0](LICENSE).
