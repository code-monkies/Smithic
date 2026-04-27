# Contributing to Smithic

Thanks for your interest. Smithic is at `v0.3` — the swarm has shipped, the bar is "small, focused PRs" rather than sprawling rewrites.

## Setup

```bash
git clone https://github.com/code-monkies/Smithic.git
cd Smithic
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -e .[dev]
```

## Running tests

Most tests stub the Claude Agent SDK and run free.

```bash
pytest
```

To run integration tests that hit the real Claude API (costs money, requires `ANTHROPIC_API_KEY`):

```bash
SMITHIC_LIVE=1 pytest -m live
```

## Project layout

See the [README](README.md) and the `src/smithic/` tree. Brief tour of the load-bearing modules:

| Module | Role |
|---|---|
| `src/smithic/orchestrator.py` | Single-run stage runner. Owns the per-run lifecycle. |
| `src/smithic/parent.py` | Swarm coordinator (`v0.3+`). Fans out N children. |
| `src/smithic/stages/research.py` | Query generation + synthesis. Cache-aware. |
| `src/smithic/stages/score.py` | Rubric application + diversity nudge. |
| `src/smithic/stages/implement.py` | The Claude Agent SDK delegation point. |
| `src/smithic/stages/critique.py` | Independent diff review before PR. |
| `src/smithic/worktree/manager.py` | Cross-platform git worktree lifecycle (locked for swarm). |
| `src/smithic/budget/meter.py` | Hard cost ceiling enforcement. |
| `src/smithic/memory/db.py` | SQLite ledger (parent + child runs). |
| `src/smithic/memory/cache.py` | Shared research cache for swarm runs. |
| `src/smithic/mcp/registry.py` | MCP server resolution per `[research].sources`. |
| `src/smithic/mcp/custom/*` | Bundled MCP servers (Reddit, HN, Product Hunt). |
| `src/smithic/rubric/default.yaml` | Default value-scoring rubric. |
| `src/smithic/config.py` | Pydantic schema for `smithic.toml`. |
| `src/smithic/cli.py` | Typer CLI. |

## High-leverage contributions right now

- **New research-source MCP servers** under `src/smithic/mcp/custom/`. The walkthrough below shows how to add one — Lobsters, Indie Hackers, GitHub Discussions, Stack Overflow are all natural targets.
- **Domain rubrics** in `src/smithic/rubric/`. The default is a starting point, not a recommendation. Consumer SaaS vs. devtools vs. content products want different weights.
- **Stack-detection** in `src/smithic/stages/introspect.py`. New language/framework heuristics make the implementing agent's context better.

## Walkthrough: add a new research source

Say you want to add a "Lobsters" MCP server (the lobste.rs community link aggregator). Roughly 5 files touch:

1. **Build the MCP server.** Copy `src/smithic/mcp/custom/hn_server.py` as a template — same FastMCP shape, swap the URL, normalize the response. Keep the in-process 5-minute response cache so concurrent swarm children don't re-hit the API for the same query.

   ```python
   # src/smithic/mcp/custom/lobsters_server.py
   from mcp.server.fastmcp import FastMCP
   import httpx

   mcp = FastMCP("smithic-lobsters")

   @mcp.tool()
   def search_lobsters(query: str, limit: int = 25) -> list[dict]:
       # ... fetch + normalize ...
       ...

   def main() -> None:
       mcp.run()

   if __name__ == "__main__":
       main()
   ```

2. **Wire it into `src/smithic/mcp/registry.py`.** Add a new `elif source == "lobsters":` branch in `build_mcp_servers`. If the source needs a token, follow the Product Hunt pattern: skip with a `event("research.source_skipped", ...)` log line when the env var is missing.

3. **Add tests.** `tests/test_lobsters_server.py` — copy `test_hn_server.py`. Stub `httpx.Client` with a `MockTransport`; assert your normalizer produces well-formed dicts. `tests/test_mcp_registry.py` — add a case proving `build_mcp_servers(["lobsters"])` returns the right config.

4. **Update `examples/sample-target.smithic.toml`'s `[research]` block** to mention the new source name in the comment.

5. **Update the README's source list** under "How feature selection works".

A single source PR is typically ~250 LOC and stays self-contained. The orchestrator and stages don't need any changes — that's the whole point of MCP-style indirection.

## Style

- `ruff format` + `ruff check`.
- `pathlib` for all path manipulation, never raw `os.path` joins.
- `subprocess.run(..., shell=False)` always — never shell strings.
- Pydantic models for any data crossing a stage boundary; no free-text dicts.
- One assertion per test where possible.

## Commit format

Conventional commits preferred: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`. Branch naming `feat/<slug>` or `fix/<slug>`.

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
