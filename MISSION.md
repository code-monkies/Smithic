# Smithic — Mission

An autonomous feature-factory swarm: point it at a repo + a mission, it
proposes the next feature based on real market signal, implements it inside
an isolated worktree, and opens a PR. Designed for founders and small teams
who want to wake up to a triage queue of candidate PRs rather than start each
morning by deciding what to build.

## Who it's for

- **Solo founders and small teams** running a real product with a Claude Pro
  / Max subscription (or an Anthropic API key, or a cloud-routed Claude
  endpoint). They have shipping velocity targets they can't hit doing every
  feature decision themselves.
- **Open-source maintainers** of mid-sized projects who want a way to
  surface community-signal-driven feature proposals without manually scanning
  Reddit / HN / their own issue tracker every week.
- **Engineering-tool builders** experimenting with multi-agent pipelines and
  looking for a load-bearing reference implementation that survives real
  production failure modes — not a demo.

## Success looks like

- A founder kicks off `smithic run --runs 5` on Monday morning and walks
  away. Two hours later, three of those five children have opened reviewable
  PRs that solve real user pain documented in the research brief; the other
  two aborted cleanly because no candidate cleared the rubric thresholds.
- Smithic itself ships a feature per week from its own dogfooded run. The
  v0.4 / v0.5 / v0.6 work in [the backlog](docs/plans/post-v0.3-backlog.md)
  gets picked up by Smithic-on-Smithic runs as the rubric and research
  sources start surfacing them as the highest-value next features.
- The community contributes new MCP research sources (Lobsters, Indie
  Hackers, GitHub issues, support inboxes) and domain-specific rubrics
  (consumer SaaS, devtools, content products) without needing to touch the
  orchestrator. The MCP-as-plugin shape holds.
- A run that fails leaves so much diagnostic state behind that the next
  iteration knows exactly what went wrong without having to re-pay for
  another live run to find out — every stage drops an artifact, the SQLite
  ledger is the source of truth, and unexpected SDK behavior surfaces as a
  named debug file rather than a stack trace.
- Cost stays predictable. A single autonomous run on a medium-sized repo
  lands under $3 in API mode end-to-end; subscription mode is bounded by the
  token ceiling. Swarms of 3-5 children land 30-60% cache savings on
  research thanks to the shared cache.

## Out of scope

- **Becoming a SaaS.** Smithic is local-first. Users run it on their own
  machine against their own repos with their own Claude credentials. We
  don't host it.
- **Becoming a generic LLM abstraction.** Smithic delegates everything to
  the Claude Agent SDK. Re-implementing against OpenAI / Gemini / local
  models would 4x the surface area for a smaller benefit than any item on
  the backlog.
- **Replacing Claude Code.** Smithic is the *outer* loop that decides what
  to build and reviews what got built. Claude Code (via the SDK) is the
  inner loop that does the building. The two compose; they don't compete.
- **Becoming a plugin marketplace.** MCP is already the plugin system.
  Don't reinvent it. If the registry of community MCP servers needs UX,
  push that work into the MCP ecosystem.
- **Replay / time-travel debugging.** The SQLite ledger + per-stage
  artifacts give forensic readability after the fact. Live time-travel is
  not where the value is.
- **Auto-merging PRs.** Every Smithic run produces a *reviewable* artifact.
  A human pulls the trigger on merge. The label blocklist (`dev-tracked`,
  `auto-deploy`, `production`, `release`, etc.) exists to make sure that
  stays true.

## What we've learned that should shape the next feature

A real subscription-mode run against a production repo (OnlyVAT) surfaced a
class of issues that mocked tests don't catch. Future Smithic features
should make these classes of bugs cheaper to find and fix:

- **Schema strictness vs. model output drift.** Pydantic `Literal` fields
  and `extra="forbid"` repeatedly rejected perfectly-fine model output that
  used freeform language for an enum or sat extra metadata next to the
  schema. Lean lenient on inputs, strict on internal contracts.
- **Diagnostic artifacts beat retries.** Three of the four blocking issues
  were debuggable in one shot once stderr / structured-output / raw-text
  artifacts were captured to disk. Adding capture is cheaper than another
  live retry.
- **Windows codecs are a real-world surface.** `subprocess.run(text=True)`
  defaults to cp1252 on Windows; `git diff` on a real repo will produce
  bytes outside that codec the moment any file has a smart quote. UTF-8
  must be explicit everywhere we shell out and in every env we hand to a
  subprocess child.
- **Subscription mode is metered in practice.** Documentation says cost is
  `$0` for subscription auth; the SDK reports non-zero `total_cost_usd`.
  Treat USD figures as informational, token ceiling as the hard limit, and
  don't assume "unmetered" means free.
