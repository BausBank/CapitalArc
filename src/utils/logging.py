"""Structured logging setup for CapitalArc, built on loguru."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from rich.console import Console

# Compact format for terminal output: level + message only.
_STDERR_FORMAT = "<level>{level: <7}</level> | <level>{message}</level>"

# Full format for the log file.
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | "
    "{name}:{function}:{line} | {message}"
)

# Module prefixes that produce high-volume Dune / data-plane INFO noise.
# Their output goes to the log file only; terminal shows WARNING+ only.
_DUNE_MODULES = {
    "src.data.dune_mcp",
    "src.data.dune_market_data",
    "src.core.level2",        # "Loaded X query ID" at connect time
    "src.data.hyperliquid_intelligence",
}


def _is_dune(record: dict) -> bool:
    return any(record["name"].startswith(m) for m in _DUNE_MODULES)


def _is_not_dune(record: dict) -> bool:
    return not _is_dune(record)


def configure_logging(
    level: str = "INFO",
    rich_console: "Console | None" = None,
) -> None:
    """Reset loguru handlers and install sink pair.

    - All Dune/data-plane logs  → logs/capitalarc.log (full detail, DEBUG+)
    - Dune WARNING+             → terminal (compact, surfaced for operator)
    - Everything else           → terminal at configured level (compact)

    When *rich_console* is provided the terminal sinks write through the
    Rich Console object instead of sys.stderr directly. This prevents log
    lines from interleaving with Rich Progress / Status spinners.
    """
    logger.remove()

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # ── File sink: full detail, all modules, DEBUG and above ──────────────
    logger.add(
        log_dir / "capitalarc.log",
        level="DEBUG",
        format=_FILE_FORMAT,
        rotation="10 MB",
        retention=3,
        backtrace=False,
        diagnose=False,
        encoding="utf-8",
    )

    # ── Terminal sinks ─────────────────────────────────────────────────────
    if rich_console is not None:
        # Route through Rich Console so Progress/Status spinners stay clean.
        def _make_sink(console: "Console"):
            def _sink(message: str) -> None:
                # message already formatted by loguru (no ANSI when colorize=False)
                console.print(str(message), end="", markup=False, highlight=False)
            return _sink

        sink = _make_sink(rich_console)

        # Only non-Dune modules go to terminal — Dune logs stay in file only.
        logger.add(
            sink,
            level=level.upper(),
            format=_STDERR_FORMAT,
            filter=_is_not_dune,
            colorize=False,
            backtrace=False,
            diagnose=False,
        )
    else:
        logger.add(
            sys.stderr,
            level=level.upper(),
            format=_STDERR_FORMAT,
            filter=_is_not_dune,
            backtrace=False,
            diagnose=False,
        )


__all__ = ["configure_logging", "logger"]
