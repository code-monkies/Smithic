"""Smithic CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console
from rich.table import Table

from smithic import __version__
from smithic.config import load_config
from smithic.orchestrator import run_once
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


@app.command("run")
def cmd_run(
    config: Annotated[Path, typer.Option("--config", "-c", exists=True, dir_okay=False, help="Path to smithic.toml.")],
    feature: Annotated[
        str | None,
        typer.Option(
            "--feature",
            "-f",
            help="Feature description (v0.1 escape hatch — required until v0.2 ships autonomous ideation).",
        ),
    ] = None,
    max_usd: Annotated[
        float | None,
        typer.Option("--max-usd", help="Override the per-run USD ceiling from config."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the Claude model used by the implement stage."),
    ] = None,
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Cap turns the implement agent can take.", min=1, max=200),
    ] = 40,
) -> None:
    """Run the Smithic pipeline once against a target repo."""
    cfg, config_dir = load_config(config)
    if max_usd is not None:
        cfg.budget.max_usd_per_run = max_usd

    if feature is None:
        _console.print(
            "[bold red]error:[/] --feature is required in v0.1. "
            "Autonomous feature ideation lands in v0.2."
        )
        raise typer.Exit(code=2)

    db_path = _resolve_db(config_dir)

    outcome = anyio.run(
        run_once,
        cfg,
        config_dir,
        feature,
        db_path,
        model,
        max_turns,
    )

    table = Table(title=f"Smithic run {outcome.run_id}", show_header=False)
    table.add_row("status", outcome.status)
    table.add_row("branch", outcome.branch or "-")
    table.add_row("PR", outcome.pr_url or "-")
    table.add_row("spent", f"${outcome.cost_usd:.4f}")
    table.add_row("notes", outcome.notes)
    _console.print(table)

    if outcome.status not in {"completed", "budget_exceeded"}:
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
        "SELECT id, status, branch, pr_url, started_at, finished_at "
        "FROM runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        _console.print("[dim]no runs recorded yet[/]")
        return

    table = Table(title="Smithic recent runs")
    for col in ("run_id", "status", "branch", "PR", "started", "finished"):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row["id"],
            row["status"],
            row["branch"] or "-",
            row["pr_url"] or "-",
            row["started_at"],
            row["finished_at"] or "-",
        )
    _console.print(table)


@app.command("clean")
def cmd_clean(
    config: Annotated[Path, typer.Option("--config", "-c", exists=True, dir_okay=False)],
    force: Annotated[bool, typer.Option("--force", help="Pass --force to git worktree remove.")] = False,
) -> None:
    """Remove all Smithic-managed worktrees from the target repo."""
    cfg, config_dir = load_config(config)
    target = cfg.target.resolve_path(config_dir)
    wt_manager = WorktreeManager(target, cfg.swarm.worktree_root)

    paths = wt_manager.list()
    if not paths:
        _console.print("[dim]no Smithic worktrees to clean[/]")
        return

    from smithic.worktree.manager import Worktree

    for path in paths:
        # We don't track the branch per-path here (would need to query git), so
        # the Worktree wrapper takes a placeholder branch — `git worktree remove`
        # only uses the path.
        worktree = Worktree(path=path, branch="", base_branch="")
        try:
            wt_manager.remove(worktree, force=force)
            _console.print(f"removed {path}")
        except Exception as exc:  # noqa: BLE001
            _console.print(f"[red]failed to remove {path}[/]: {exc}")


if __name__ == "__main__":
    app()
