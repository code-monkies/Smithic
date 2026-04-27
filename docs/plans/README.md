# Smithic plans

Self-contained release plans that can be handed to a fresh Claude (or human) instance to execute. Each plan assumes only the current state of the repo plus what's written in the file.

| Plan | Status | Scope |
|---|---|---|
| `v0.1` (initial) | shipped | Plumbing-first: end-to-end pipeline with feature description supplied via `--feature`. |
| [`v0.2-autonomous-ideation.md`](v0.2-autonomous-ideation.md) | shipped | Research stage (web + Reddit), value scoring, self-critique, abort threshold. After this, `--feature` becomes optional. |
| [`v0.3-swarm.md`](v0.3-swarm.md) | shipped | Parallel runs via worktrees, cross-run research cache, HN + Product Hunt MCPs, polish. |
| [`post-v0.3-backlog.md`](post-v0.3-backlog.md) | parking lot | Sketches for observability, vector cache, private signals, scheduling, distributed swarms. Lift into a real plan when picking up. |

## How to execute a plan

1. Read the plan front-to-back before changing anything.
2. Read the entire current `src/smithic/` tree so you know what's actually there.
3. Read `README.md` and `CONTRIBUTING.md`.
4. Follow the suggested commit boundaries — small commits make things easier to bisect when something breaks.
5. Keep the test suite green at every commit.
6. When done, update the README roadmap and the status column above.
