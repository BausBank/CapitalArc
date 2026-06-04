"""DEPRECATED shim - the backtester was promoted to ``core/backtest/`` (Stage 1).

Kept so existing muscle memory / scripts keep working. Delegates to the
package CLI. Prefer:

    python -m core.backtest.runner [--refresh] [--splits N]
"""

from __future__ import annotations

from core.backtest.runner import main

if __name__ == "__main__":
    main()
