"""Tiny structured logger — JSON lines to stdout, human lines to stderr.

Smithic logs are intended to be both grep-friendly for automation and readable
when watching a run live. We write a structured JSON record to ``stdout`` and a
short human summary to ``stderr`` (which Rich can pretty-print).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from rich.console import Console

_console = Console(stderr=True, highlight=False)


def event(name: str, **fields: Any) -> None:
    """Emit one structured event."""
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": name,
        **fields,
    }
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()
    summary_bits = [f"{k}={v}" for k, v in fields.items() if k in {"run_id", "stage", "status"}]
    summary = " ".join(summary_bits)
    _console.print(f"[dim]{record['ts']}[/] [cyan]{name}[/] {summary}")
