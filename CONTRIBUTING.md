# Contributing to Smithic

Thanks for your interest. Smithic is in early `v0.1` so the bar is "small, focused PRs" rather than sprawling rewrites.

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
| `src/smithic/orchestrator.py` | Linear stage runner. Owns the run lifecycle. |
| `src/smithic/stages/implement.py` | The Claude Agent SDK delegation point. |
| `src/smithic/stages/critique.py` | Quality gate before PR (`v0.2+`). |
| `src/smithic/worktree/manager.py` | Cross-platform git worktree lifecycle. |
| `src/smithic/budget/meter.py` | Hard cost ceiling enforcement. |
| `src/smithic/rubric/default.yaml` | Default value-scoring rubric (`v0.2+`). |
| `src/smithic/config.py` | Pydantic schema for `smithic.toml`. |
| `src/smithic/cli.py` | Typer CLI. |

## High-leverage `v0.1`-era contributions

- **Stack-detection improvements** in `src/smithic/stages/introspect.py`. Smithic detects what kind of project a target repo is so it can give the implementing agent appropriate context. New language/framework heuristics are valuable.
- **Custom MCP servers** under `src/smithic/mcp/custom/`. Reddit, HN, Product Hunt are scaffolded but `v0.1` doesn't wire them in yet — `v0.2` does. PRs that polish them are welcome.
- **Rubric experiments** in `src/smithic/rubric/`. The default rubric is a starting point, not a recommendation. Domain-specific rubrics (consumer SaaS vs. devtools vs. content products) would be useful additions.

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
