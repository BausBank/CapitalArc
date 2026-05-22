"""Shared pytest configuration for the CapitalArc test suite.

`pytest-asyncio` 0.23 requires every coroutine test to be explicitly
marked with `@pytest.mark.asyncio` (the test files already are). We
also pin the asyncio mode to `strict` for clarity and ensure the
project root is on `sys.path` regardless of how pytest is invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
