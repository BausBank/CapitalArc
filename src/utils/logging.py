"""Structured logging setup for CapitalArc, built on loguru.

Two output regimes:

* `verbose=True` mirrors the development view - every loguru sink
  prints to stderr at the configured level (Dune POST chatter,
  retry attempts, execution_id, poll loops, etc).
* `verbose=False` (default) suppresses all chatter below `WARNING`
  on stderr so the retro-style CLI built on top stays clean. The
  full log is still kept in-memory via a rotating file sink at
  `logs/capitalarc.log` so post-mortem debugging is one
  `tail -f logs/capitalarc.log` away.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


_LOG_DIR = Path("logs")
_LOG_FILE = _LOG_DIR / "capitalarc.log"


def configure_logging(level: str = "INFO", *, verbose: bool = True) -> None:
    """Reset loguru handlers and install the configured sinks.

    Parameters
    ----------
    level :
        Minimum level for the stderr sink when `verbose=True`, and
        the file sink in either mode.
    verbose :
        When True, the stderr sink mirrors the configured `level`.
        When False, the stderr sink is silenced down to `ERROR` so
        the curated retro CLI is the only thing the user sees.
    """
    logger.remove()

    stderr_level = level.upper() if verbose else "ERROR"
    logger.add(
        sys.stderr,
        level=stderr_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )

    try:
        _LOG_DIR.mkdir(exist_ok=True)
        logger.add(
            _LOG_FILE,
            level=level.upper(),
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | "
                "{name}:{function}:{line} | {message}"
            ),
            rotation="5 MB",
            retention=3,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )
    except OSError:
        # Disk write failures must never crash the agent loop -
        # the stderr sink is already attached, that's enough.
        pass


__all__ = ["configure_logging", "logger"]
