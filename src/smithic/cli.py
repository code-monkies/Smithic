"""Smithic CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console
from rich.table import Table

from smithic import __version__
from smithic.auth import AuthError
from smithic.config import load_config
from smithic.orchestrator import run_once
from smithic.parent import run_swarm
from smithic.worktree.manager import WorktreeManager

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Smithic — autonomous feature-factory swarm. Point it at a repo. It opens PRs.",
)

_console = Console()


def _version_callback(value: bool) -> None:
    if value:
        _console.print(f"smithic {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Print version and exit."),
    ] = None,
) -> None:
    return


def _resolve_db(config_dir: Path) -> Path:
    smithic_dir = config_dir / ".smithic"
    smithic_dir.mkdir(parents=True, exist_ok=True)
    return smithic_dir / "smithic.db"


def _resolve_cache_db(config_dir: Path) -> Path:
    smithic_dir = config_dir / ".smithic"
    smithic_dir.mkdir(parents=True, exist_ok=True)
    return smithic_dir / "research_cache.db"


@app.command("run")
def cmd_run(
    config: Annotated[Path, typer.Option("--config", "-c", exists=True, dir_okay=False, help="Path to smithic.toml.")],
    feature: Annotated[
        str | None,
        typer.Option(
            "--feature",
            "-f",
            help="Feature description. Omit to let Smithic pick via the research+score loop.",
        ),
    ] = None,
    runs: Annotated[
        int,
        typer.Option(
            "--runs",
            "-n",
            help="Number of parallel child runs in this invocation (1 = single run).",
            min=1,
            max=20,
        ),
    ] = 1,
    rubric: Annotated[
        Path | None,
        typer.Option(
            "--rubric",
            help="Override [rubric].path. YAML file merged on top of the bundled default.",
        ),
    ] = None,
    research_only: Annotated[
        bool,
        typer.Option(
            "--research-only",
            help="Write a research brief to <target>/.smithic/ and exit. No worktree, no PR.",
        ),
    ] = False,
    no_critique: Annotated[
        bool,
        typer.Option(
            "--no-critique",
            help="Skip the critic stage. For debugging only — disables the v0.2 safety net.",
        ),
    ] = False,
    max_usd: Annotated[
        float | None,
        typer.Option("--max-usd", help="Override the per-run USD ceiling from config."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the Claude model used by all stages."),
    ] = None,
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Cap turns the implement agent can take.", min=1, max=200),
    ] = 150,
    auth_mode: Annotated[
        str | None,
        typer.Option(
            "--auth-mode",
            help="Override [auth].mode from config: auto, api, subscription, bedrock, vertex, foundry.",
        ),
    ] = None,
) -> None:
    """Run the Smithic pipeline against a target repo.

    Pass ``--runs N`` (with N > 1) to fan out to N parallel children sharing
    one research cache. Each child opens its own PR; one child failing does
    not abort siblings.
    """
    cfg, config_dir = load_config(config)
    if max_usd is not None:
        cfg.budget.max_usd_per_run = max_usd
    if auth_mode is not None:
        cfg.auth = cfg.auth.model_copy(update={"mode": auth_mode})
    if rubric is not None:
        cfg.rubric = cfg.rubric.model_copy(update={"path": rubric})
    if no_critique:
        cfg.critique = cfg.critique.model_copy(update={"enable": False})

    if research_only and runs > 1:
        _console.print("[bold red]error:[/] --research-only is incompatible with --runs > 1.")
        raise typer.Exit(code=2)

    db_path = _resolve_db(config_dir)

    if runs == 1:
        _run_single(cfg, config_dir, feature, db_path, model, max_turns, research_only)
    else:
        _run_swarm(cfg, config_dir, feature, db_path, runs, model, max_turns)


def _run_single(
    cfg,
    config_dir: Path,
    feature: str | None,
    db_path: Path,
    model: str | None,
    max_turns: int,
    research_only: bool,
) -> None:
    async def _entry() -> object:
        return await run_once(
            config=cfg,
            config_dir=config_dir,
            feature_seed=feature,
            db_path=db_path,
            model=model,
            max_turns=max_turns,
            research_only=research_only,
        )

    try:
        outcome = anyio.run(_entry)
    except AuthError as exc:
        _console.print(f"[bold red]auth error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title=f"Smithic run {outcome.run_id}", show_header=False)
    table.add_row("status", outcome.status)
    table.add_row("branch", outcome.branch or "-")
    table.add_row("PR", outcome.pr_url or "-")
    table.add_row("spent", f"${outcome.cost_usd:.4f}")
    table.add_row("notes", outcome.notes)
    _console.print(table)

    if outcome.status not in {"completed", "budget_exceeded"}:
        raise typer.Exit(code=1)


def _run_swarm(
    cfg,
    config_dir: Path,
    feature: str | None,
    db_path: Path,
    runs: int,
    model: str | None,
    max_turns: int,
) -> None:
    async def _entry() -> object:
        return await run_swarm(
            config=cfg,
            config_dir=config_dir,
            feature_seed=feature,
            db_path=db_path,
            n_runs=runs,
            model=model,
            max_turns=max_turns,
        )

    try:
        swarm = anyio.run(_entry)
    except AuthError as exc:
        _console.print(f"[bold red]auth error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title=f"Smithic swarm {swarm.parent_run_id} — {swarm.status}")
    for col in ("run_id", "status", "branch", "PR", "spent", "notes"):
        table.add_column(col)
    for o in swarm.outcomes:
        table.add_row(
            o.run_id,
            o.status,
            o.branch or "-",
            o.pr_url or "-",
            f"${o.cost_usd:.4f}",
            (o.notes or "")[:80],
        )
    _console.print(table)
    _console.print(
        f"[bold]{len(swarm.successful)}/{swarm.n_runs}[/] succeeded · "
        f"total spent: ${swarm.total_cost_usd:.4f}"
    )

    if swarm.status == "error":
        raise typer.Exit(code=1)


@app.command("status")
def cmd_status(
    config: Annotated[Path, typer.Option("--config", "-c", exists=True, dir_okay=False)],
    limit: Annotated[int, typer.Option("--limit", help="How many recent runs to show.")] = 20,
) -> None:
    """Show the most recent runs and their outcomes."""
    _, config_dir = load_config(config)
    db_path = _resolve_db(config_dir)
    if not db_path.exists():
        _console.print("[dim]no runs recorded yet[/]")
        return

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, status, branch, pr_url, started_at, finished_at, "
        "selected_candidate_title, critic_verdict, parent_run_id "
        "FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        _console.print("[dim]no runs recorded yet[/]")
        return

    table = Table(title="Smithic recent runs")
    for col in ("run_id", "parent", "status", "branch", "PR", "selected", "critic", "started"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row["id"],
            (row["parent_run_id"] or "-")[-8:],
            row["status"],
            row["branch"] or "-",
            row["pr_url"] or "-",
            row["selected_candidate_title"] or "-",
            row["critic_verdict"] or "-",
            row["started_at"],
        )
    _console.print(table)


@app.command("clean")
def cmd_clean(
    config: Annotated[Path, typer.Option("--config", "-c", exists=True, dir_okay=False)],
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Remove every Smithic worktree under this target (default).",
        ),
    ] = False,
    keep_failed: Annotated[
        bool,
        typer.Option(
            "--keep-failed",
            help="Only remove worktrees from runs that completed; keep error/aborted ones for inspection.",
        ),
    ] = False,
    cache: Annotated[
        bool,
        typer.Option(
            "--cache",
            help="Also drop the research cache for this target. Use after schema changes or to force a fresh probe.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Pass --force to git worktree remove.")] = False,
) -> None:
    """Remove Smithic-managed worktrees and optionally the research cache.

    By default (or with ``--all``), every worktree under the target is
    removed. ``--keep-failed`` only cleans up worktrees whose run finished
    with status ``completed`` so you can still inspect the broken ones.
    """
    cfg, config_dir = load_config(config)
    target = cfg.target.resolve_path(config_dir)
    wt_manager = WorktreeManager(target, cfg.swarm.worktree_root)

    if cache:
        from smithic.memory.cache import ResearchCache

        cache_db = _resolve_cache_db(config_dir)
        if cache_db.exists():
            removed = ResearchCache(cache_db).clear(target)
            _console.print(f"cleared {removed} cache entr{'y' if removed == 1 else 'ies'}")
        else:
            _console.print("[dim]no cache to clear[/]")

    paths = wt_manager.list()
    if not paths:
        _console.print("[dim]no Smithic worktrees to clean[/]")
        return

    keep_runs: set[str] = set()
    if keep_failed:
        keep_runs = _failed_run_ids(_resolve_db(config_dir))

    from smithic.worktree.manager import Worktree

    for path in paths:
        if keep_failed and _path_run_id(path) in keep_runs:
            _console.print(f"keeping {path} (run did not complete cleanly)")
            continue
        worktree = Worktree(path=path, branch="", base_branch="")
        try:
            wt_manager.remove(worktree, force=force)
            _console.print(f"removed {path}")
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]failed to remove {path}[/]: {exc}")


def _failed_run_ids(db_path: Path) -> set[str]:
    """Run IDs that finished with anything but ``completed``."""
    if not db_path.exists():
        return set()
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, status FROM runs WHERE status != 'completed'"
        ).fetchall()
    finally:
        conn.close()
    return {row["id"] for row in rows}


def _path_run_id(path: Path) -> str:
    """The leaf directory of a Smithic worktree path encodes the run_id."""
    return path.name


if __name__ == "__main__":
    app()
