"""Rich console rendering helpers for the CapitalArc agent loop.

Builds a consistent set of panels that the `main.py` orchestrator
prints on every decision cycle:

    1. Market Context     - what data went into the engine.
    2. Level 1            - hard-rule outcomes per symbol + decision.
    3. Level 2            - on-chain intelligence per symbol + heat.
    4. Final Decision     - aggregated score, regime, directive.
    5. Execution Plan     - what the AllocationRouter intends to do.
    6. On-chain Result    - tx hashes + explorer links per submitted tx.

The helpers are pure: they take the structured results and return
`rich` renderables. `main.py` owns the `Console` instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
)
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Retro / clean-mode UI primitives
# ---------------------------------------------------------------------------


# Tag emoji palette used by the clean output. Centralised so the demo
# can stay consistent across helpers.
_EMOJI_OK = "[green]\u2713[/]"  # ✓
_EMOJI_FAIL = "[red]\u2717[/]"  # ✗
_EMOJI_WARN = "[yellow]\u26a0[/]"  # ⚠
_EMOJI_UP = "[bold green]\U0001f4c8[/]"  # 📈
_EMOJI_DOWN = "[bold red]\U0001f4c9[/]"  # 📉


def _now_local() -> datetime:
    return datetime.now()


def print_retro_header(console: Console) -> None:
    """Single-shot greeting printed once per process.

    Format intentionally evokes early-2000s sysadmin terminals:

        [ 2026-05-21 14:21:18 ] CapitalArc starting ...
    """
    ts = _now_local().strftime("%Y-%m-%d %H:%M:%S")
    console.print(
        f"[bold cyan][[/] [bold]{ts}[/] [bold cyan]][/] "
        f"[bold magenta]CapitalArc[/] [dim]starting ...[/]"
    )


def print_section(console: Console, label: str) -> None:
    """Print a top-level retro section header (e.g. `> Loading Level 2 data...`)."""
    console.print(f"[bold cyan]>[/] [bold]{label}[/]")


def print_note(console: Console, label: str, *, style: str = "yellow") -> None:
    """Print an inline retro-style note prefixed with `>`."""
    console.print(f"[bold cyan]>[/] [{style}]{label}[/]")


def print_cycle_summary(
    console: Console,
    *,
    action: str,
    bias: str,
    bias_strength: float,
    score: float,
    symbol: str | None,
) -> None:
    """One-line cycle headline rendered before the detailed panels.

    Example::

        > DECISION: RISK_OFF | Bias: BEARISH (0.72) | Score: 0.31 | Action: CLOSE BTC-PERP
    """
    decision_label = action.upper()
    bias_label = bias.upper()
    bias_color = _colour_for_bias(bias)

    if decision_label.startswith("OPEN_LONG"):
        decision_color = "bold green"
        emoji = _EMOJI_UP
        action_label = f"OPEN LONG {symbol}" if symbol else "OPEN LONG"
        action_color = "bold green"
        regime_label = "RISK_ON"
    elif decision_label.startswith("OPEN_SHORT"):
        decision_color = "bold red"
        emoji = _EMOJI_DOWN
        action_label = f"OPEN SHORT {symbol}" if symbol else "OPEN SHORT"
        action_color = "bold red"
        regime_label = "RISK_ON"
    elif decision_label == "CLOSE":
        decision_color = "bold yellow"
        emoji = _EMOJI_WARN
        action_label = f"CLOSE {symbol}" if symbol else "CLOSE"
        action_color = "yellow"
        regime_label = "RISK_OFF"
    elif decision_label == "DENY":
        decision_color = "bold red"
        emoji = _EMOJI_FAIL
        action_label = "DENY"
        action_color = "bold red"
        regime_label = "RISK_OFF"
    else:  # hold / unknown
        decision_color = "bold white"
        emoji = "\U0001f4ca"  # 📊
        action_label = decision_label
        action_color = "white"
        regime_label = "HOLD"

    console.print(
        f"[bold cyan]>[/] {emoji} "
        f"[bold]DECISION:[/] [{decision_color}]{regime_label}[/] [dim]|[/] "
        f"[bold]Bias:[/] [{bias_color}]{bias_label}[/] "
        f"({bias_strength:.2f}) [dim]|[/] "
        f"[bold]Score:[/] [bold]{score:.3f}[/] [dim]|[/] "
        f"[bold]Action:[/] [{action_color}]{action_label}[/]"
    )


def print_market_bias_summary(
    console: Console,
    *,
    bias: str,
    strength: float,
) -> None:
    """One-line on-chain bias readout (used after the L2 progress block)."""
    bias_label = bias.upper()
    bias_color = _colour_for_bias(bias)
    if bias.lower() == "bullish":
        emoji = _EMOJI_UP
    elif bias.lower() == "bearish":
        emoji = _EMOJI_DOWN
    else:
        emoji = "\u27a1\ufe0f"  # ➡️
    console.print(
        f"[bold cyan]>[/] {emoji} "
        f"[bold]Market Bias:[/] [{bias_color}]{bias_label}[/] "
        f"(strength=[bold]{strength:.2f}[/])"
    )


# ---------------------------------------------------------------------------
# Metric progress reporter (rich.progress wrapper)
# ---------------------------------------------------------------------------


_METRIC_LABEL_WIDTH = 20


class MetricProgressReporter:
    """Thin wrapper around `rich.progress.Progress` for metric fetches.

    Each metric advances through up to two hops:

        start  ->  submitted  ->  completed

    A "cached" or "n/a" event also lands in the terminal state (full
    bar) but is colour-coded so the user can tell real Dune rows from
    cached / not-configured ones at a glance. An "error" event paints
    the bar red and freezes it at its last position.
    """

    # Map abstract DuneMCPClient events to the progress steps we want
    # the bar to show. 2 steps matches the user's `(2/2)` example.
    _TOTAL = 2

    def __init__(self, console: Console, metrics: list[str]) -> None:
        self._console = console
        self._metrics = metrics
        self._progress = Progress(
            TextColumn("  {task.description}"),
            BarColumn(
                bar_width=22,
                complete_style="cyan",
                finished_style="bold green",
                pulse_style="cyan",
            ),
            TextColumn(
                "[progress.percentage]{task.percentage:>3.0f}%",
                justify="right",
            ),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
            transient=False,
            refresh_per_second=20,
        )
        self._tasks: dict[str, int] = {}
        self._terminal: dict[str, str] = {}
        self._started = False

    # ---- Lifecycle -----------------------------------------------------

    def __enter__(self) -> "MetricProgressReporter":
        self._progress.__enter__()
        self._started = True
        for metric in self._metrics:
            label = self._format_label(metric, "white")
            task_id = self._progress.add_task(
                label, total=self._TOTAL, completed=0
            )
            self._tasks[metric] = task_id
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._progress.__exit__(exc_type, exc, tb)
        self._started = False

    # ---- Hook callable -------------------------------------------------

    def callback(self) -> Callable[[str, str], None]:
        """Return a thread-safe callable suitable for `DuneMCPClient.on_metric_event`."""
        return self.handle_event

    def handle_event(self, metric: str, event: str) -> None:
        if not self._started:
            return
        task_id = self._tasks.get(metric)
        if task_id is None:
            return
        if event == "start":
            self._progress.update(task_id, completed=0)
        elif event == "submitted":
            self._progress.update(task_id, completed=1)
        elif event == "completed":
            self._progress.update(
                task_id,
                completed=self._TOTAL,
                description=self._format_label(metric, "green", suffix="OK"),
            )
            self._terminal[metric] = "ok"
        elif event == "cached":
            self._progress.update(
                task_id,
                completed=self._TOTAL,
                description=self._format_label(metric, "cyan", suffix="cached"),
            )
            self._terminal[metric] = "cached"
        elif event == "n/a":
            self._progress.update(
                task_id,
                completed=self._TOTAL,
                description=self._format_label(
                    metric, "yellow", suffix="n/a"
                ),
            )
            self._terminal[metric] = "n/a"
        elif event == "error":
            self._progress.update(
                task_id,
                description=self._format_label(metric, "red", suffix="err"),
            )
            self._terminal[metric] = "error"

    # ---- Helpers -------------------------------------------------------

    def _format_label(
        self, metric: str, color: str, *, suffix: str = ""
    ) -> str:
        base = metric.ljust(_METRIC_LABEL_WIDTH)
        tag = f" [dim]\u2014 {suffix}[/]" if suffix else ""
        return f"[{color}]{base}[/]{tag}"


def metric_progress(
    console: Console, metrics: list[str]
) -> MetricProgressReporter:
    """Convenience factory mirroring the other helpers in this module."""
    return MetricProgressReporter(console, metrics)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def _colour_for_trend(trend: str) -> str:
    return {
        "up": "bold green",
        "down": "bold red",
        "mixed": "yellow",
        "flat": "white",
    }.get(trend, "white")


def _colour_for_severity(sev: str) -> str:
    return {
        "block": "bold red",
        "warn": "yellow",
        "info": "cyan",
    }.get(sev, "white")


def _colour_for_regime(regime: str) -> str:
    return {
        "risk_on": "bold green",
        "risk-on": "bold green",
        "risk_off": "bold red",
        "risk-off": "bold red",
        "neutral": "yellow",
        "transition": "yellow",
        "hold": "yellow",
    }.get(regime, "white")


def _colour_for_bias(bias: str) -> str:
    return {
        "bullish": "bold green",
        "bearish": "bold red",
        "neutral": "yellow",
    }.get(bias, "white")


def _colour_for_side(side: str | None) -> str:
    if not side:
        return "white"
    return {
        "long": "bold green",
        "short": "bold red",
        "flat": "yellow",
    }.get(side.lower(), "white")


def _fmt_pct(x: float, precision: int = 2) -> str:
    return f"{x:+.{precision}f}%"


def _fmt_usd(x: float | Decimal | None) -> str:
    if x is None:
        return "-"
    val = float(x)
    if abs(val) >= 1_000_000_000:
        return f"${val / 1e9:.2f}B"
    if abs(val) >= 1_000_000:
        return f"${val / 1e6:.2f}M"
    if abs(val) >= 1_000:
        return f"${val / 1e3:.2f}K"
    return f"${val:,.2f}"


def _fmt_num(x: float, precision: int = 2) -> str:
    if abs(x) >= 1_000_000_000:
        return f"{x / 1e9:.2f}B"
    if abs(x) >= 1_000_000:
        return f"{x / 1e6:.2f}M"
    if abs(x) >= 1_000:
        return f"{x / 1e3:.2f}K"
    return f"{x:.{precision}f}"


# ---------------------------------------------------------------------------
# Panel: Market Context
# ---------------------------------------------------------------------------


def market_context_panel(
    context: dict[str, Any],
    *,
    mode: str,
    app_env: str,
    timestamp: datetime | None = None,
) -> Panel:
    ts = timestamp or datetime.now(timezone.utc)
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column(style="white")

    table.add_row("Time (UTC)", ts.strftime("%Y-%m-%d %H:%M:%S"))
    table.add_row("Mode", f"[bold]{mode}[/] (env={app_env})")
    table.add_row("Symbols", ", ".join(context.get("symbols", []) or []))
    table.add_row("RPC (account state)", context.get("rpc_url", "-"))
    dune_chain = context.get("dune_chain")
    if dune_chain:
        table.add_row("Dune chain", f"[bold]{dune_chain}[/] (dex.trades)")
    dune_tokens = context.get("dune_tokens") or {}
    if dune_tokens:
        short = ", ".join(
            f"{sym.split('-')[0]}={addr[:6]}.." for sym, addr in dune_tokens.items()
        )
        table.add_row("Token map", short)
    if account := context.get("account_address"):
        table.add_row("Agent wallet", account)
    if account_id := context.get("account_id"):
        table.add_row("Account ID", account_id)
    timeframes = context.get("l1_timeframes")
    if timeframes:
        table.add_row("L1 timeframes", ", ".join(timeframes))
    margin = context.get("account_margin_usdc")
    pnl = context.get("account_unrealized_pnl_usdc")
    if margin is not None:
        table.add_row("Agent margin", _fmt_usd(margin))
    if pnl is not None:
        pnl_v = float(pnl)
        pnl_colour = "green" if pnl_v >= 0 else "red"
        table.add_row("Unrealized PnL", Text(_fmt_usd(pnl_v), style=pnl_colour))
    dd = context.get("account_drawdown_pct")
    if dd is not None:
        dd_v = float(dd)
        dd_colour = "white" if dd_v < 5 else "yellow" if dd_v < 10 else "red"
        table.add_row("Drawdown", Text(f"{dd_v:.2f}%", style=dd_colour))
    onchain = context.get("onchain")
    if onchain is not None:
        table.add_row(
            "Arc latest block", str(getattr(onchain, "latest_block", "-") or "-")
        )
        tvl = getattr(onchain, "vault_tvl_usdc", None)
        if tvl is not None:
            table.add_row("Vault TVL", _fmt_usd(tvl))
    return Panel(
        table,
        title="[bold]Market Context[/]",
        border_style="cyan",
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Panel: Level 1
# ---------------------------------------------------------------------------


def level1_panel(l1_raw: dict[str, Any], score: float) -> Panel:
    passes = l1_raw.get("passes", True)
    per_symbol = l1_raw.get("per_symbol", {}) or {}
    rationale = l1_raw.get("rationale", "")

    indicators = Table(
        title="Indicators (latest candle)",
        box=box.MINIMAL_HEAVY_HEAD,
        expand=True,
        show_lines=False,
    )
    indicators.add_column("Symbol", style="bold")
    indicators.add_column("TF", style="cyan")
    indicators.add_column("Close", justify="right")
    indicators.add_column("EMA9", justify="right")
    indicators.add_column("EMA21", justify="right")
    indicators.add_column("RSI", justify="right")
    indicators.add_column("ATR%", justify="right")
    indicators.add_column("Trend")

    for sym, readout in per_symbol.items():
        for row in readout.get("rows", []) or []:
            rsi = float(row.get("rsi", 0))
            rsi_color = "red" if rsi >= 70 else "red" if rsi <= 30 else "white"
            trend = row.get("trend", "flat")
            indicators.add_row(
                sym,
                str(row.get("timeframe", "")),
                _fmt_num(float(row.get("close", 0)), 2),
                _fmt_num(float(row.get("ema_fast", 0)), 2),
                _fmt_num(float(row.get("ema_slow", 0)), 2),
                f"[{rsi_color}]{rsi:.1f}[/]",
                f"{float(row.get('atr_pct', 0)):.2f}%",
                Text(trend, style=_colour_for_trend(trend)),
            )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", justify="right")
    summary.add_column(style="white")
    verdict = "[bold green]PASS[/]" if passes else "[bold red]BLOCKED[/]"
    summary.add_row("Verdict", verdict)
    summary.add_row("Score", f"{score:.3f}")
    summary.add_row("Rationale", rationale or "-")

    reasons = l1_raw.get("reasons") or []
    reasons_table = Table(
        title="Reasons",
        box=box.MINIMAL,
        expand=True,
        show_lines=False,
        show_header=True,
    )
    reasons_table.add_column("Sev", width=6)
    reasons_table.add_column("Code", style="bold")
    reasons_table.add_column("Symbol", style="cyan")
    reasons_table.add_column("TF", style="cyan", width=4)
    reasons_table.add_column("Message")

    if reasons:
        for r in reasons:
            sev = r.get("severity", "info")
            reasons_table.add_row(
                Text(sev.upper(), style=_colour_for_severity(sev)),
                r.get("code", ""),
                r.get("symbol") or "-",
                r.get("timeframe") or "-",
                r.get("message", ""),
            )
    else:
        reasons_table.add_row("-", "-", "-", "-", "no rules triggered")

    return Panel(
        Group(summary, indicators, reasons_table),
        title="[bold]Level 1 - Technical Hard Rules (OHLCV via Dune MCP)[/]",
        border_style="green" if passes else "red",
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Panel: Level 2
# ---------------------------------------------------------------------------


def level2_panel(
    l2_raw: dict[str, Any],
    score: float,
    *,
    short_circuit_note: str | None = None,
) -> Panel:
    if l2_raw.get("skipped"):
        return Panel(
            Text("Level 2 skipped (Level 1 short-circuit).", style="yellow"),
            title="[bold]Level 2 - On-chain Intelligence (skipped)[/]",
            border_style="yellow",
            box=box.ROUNDED,
        )

    regime = l2_raw.get("regime", "neutral")
    heat = float(l2_raw.get("market_heat", 0.5))
    cached = bool(l2_raw.get("cached", False))
    per_symbol = l2_raw.get("per_symbol", {}) or {}
    vault = l2_raw.get("vault_flow", {}) or {}
    metric_status = l2_raw.get("metric_status", {}) or {}
    notes = l2_raw.get("notes") or []
    dune_healthy = bool(l2_raw.get("dune_healthy", False))

    # ---------- Market summary -----------------------------------------
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", justify="right")
    summary.add_column(style="white")
    heat_source = l2_raw.get("heat_source", "heuristic")
    heat_src_style = "green" if heat_source.startswith("dune:") else "yellow"
    heat_src_label = heat_source if heat_source.startswith("dune:") else "heuristic (fallback)"
    summary.add_row("Regime", Text(regime, style=_colour_for_regime(regime)))
    summary.add_row("Score", f"{score:.3f}")
    summary.add_row(
        "Market heat",
        Text(f"{heat:.3f}  [{heat_src_label}]", style=heat_src_style),
    )
    bias = str(l2_raw.get("market_bias", "neutral"))
    bias_strength = float(l2_raw.get("bias_strength", 0.0) or 0.0)
    summary.add_row(
        "Market bias",
        Text(
            f"{bias.upper()}  (strength={bias_strength:.2f})",
            style=_colour_for_bias(bias),
        ),
    )
    summary.add_row("Cache", "[yellow]HIT[/]" if cached else "[green]MISS[/]")
    summary.add_row(
        "Dune MCP",
        "[green]healthy[/]" if dune_healthy else "[red]unreachable[/]",
    )

    # ---------- Per-symbol metrics --------------------------------------
    metrics = Table(
        title="Per-symbol metrics (Dune MCP)",
        box=box.MINIMAL_HEAVY_HEAD,
        expand=True,
    )
    metrics.add_column("Symbol", style="bold")
    metrics.add_column("Price", justify="right")
    metrics.add_column("24h %", justify="right")
    metrics.add_column("Funding", justify="right")
    metrics.add_column("Fund APR", justify="right")
    metrics.add_column("OI USD", justify="right")
    metrics.add_column("OI 1h", justify="right")
    metrics.add_column("OI 24h", justify="right")
    metrics.add_column("L/S", justify="right")
    metrics.add_column("Vol spike")

    for sym, intel in per_symbol.items():
        fund = intel.get("funding", {})
        oi = intel.get("open_interest", {})
        vol = intel.get("volume", {})
        lsr = intel.get("long_short", {})

        price_change = float(vol.get("price_change_pct_24h", 0))
        change_colour = (
            "bold green" if price_change > 0
            else "bold red" if price_change < 0
            else "white"
        )
        funding_rate = float(fund.get("current_rate", 0)) * 100  # %
        funding_colour = (
            "green" if funding_rate > 0
            else "red" if funding_rate < 0
            else "white"
        )
        oi_1h = float(oi.get("delta_1h_pct", 0))
        oi_1h_colour = "green" if oi_1h > 0 else "red" if oi_1h < 0 else "white"
        oi_24h = float(oi.get("delta_24h_pct", 0))
        oi_24h_colour = "green" if oi_24h > 0 else "red" if oi_24h < 0 else "white"
        ls_ratio = float(lsr.get("long_short_ratio", 1.0))
        ls_colour = (
            "green" if ls_ratio > 1.1 else "red" if ls_ratio < 0.9 else "white"
        )
        spike_text = "YES" if vol.get("spike_detected") else "no"
        spike_colour = "yellow" if vol.get("spike_detected") else "white"

        metrics.add_row(
            sym,
            _fmt_num(float(vol.get("last_price", 0)), 2),
            Text(_fmt_pct(price_change), style=change_colour),
            Text(f"{funding_rate:+.4f}%", style=funding_colour),
            f"{float(fund.get('annualised_pct', 0)):+.1f}%",
            _fmt_usd(oi.get("current_value_usd", 0)),
            Text(_fmt_pct(oi_1h), style=oi_1h_colour),
            Text(_fmt_pct(oi_24h), style=oi_24h_colour),
            Text(f"{ls_ratio:.2f}", style=ls_colour),
            Text(spike_text, style=spike_colour),
        )

    # ---------- Whales + cumulative funding -----------------------------
    whales = Table(
        title="Whale activity & cumulative funding",
        box=box.MINIMAL,
        expand=True,
    )
    whales.add_column("Symbol", style="bold")
    whales.add_column("Whale", style="yellow")
    whales.add_column("Count", justify="right")
    whales.add_column("Direction")
    whales.add_column("Notional delta", justify="right")
    whales.add_column("Cum funding (window)", justify="right")
    whales.add_column("Net longs paid", justify="right")

    for sym, intel in per_symbol.items():
        wh = intel.get("whales", {})
        cf = intel.get("cum_funding", {})
        direction = wh.get("direction", "neutral")
        dir_colour = (
            "green" if direction == "accumulating"
            else "red" if direction == "distributing"
            else "white"
        )
        whales.add_row(
            sym,
            "YES" if wh.get("flagged") else "no",
            str(int(wh.get("n_whales", 0) or 0)),
            Text(direction, style=dir_colour),
            _fmt_usd(wh.get("notional_usd_change")),
            f"{cf.get('window_hours', 0):.0f}h",
            _fmt_usd(cf.get("net_flow_usd", 0)),
        )

    # ---------- Vault flow ---------------------------------------------
    vault_table = Table.grid(padding=(0, 2))
    vault_table.add_column(style="bold cyan", justify="right")
    vault_table.add_column(style="white")
    vault_table.add_row("Vault TVL", _fmt_usd(vault.get("tvl_usdc", 0)))
    vault_table.add_row(
        "Window",
        f"{vault.get('window_hours', 0):.0f}h",
    )
    vault_table.add_row("Deposits", _fmt_usd(vault.get("deposits_usdc", 0)))
    vault_table.add_row("Withdrawals", _fmt_usd(vault.get("withdrawals_usdc", 0)))
    net = float(vault.get("net_flow_usdc", 0))
    net_colour = "green" if net > 0 else "red" if net < 0 else "white"
    vault_table.add_row("Net flow", Text(_fmt_usd(net), style=net_colour))
    vault_table.add_row(
        "Events",
        f"in {vault.get('deposit_events', 0)}  "
        f"out {vault.get('withdrawal_events', 0)}",
    )

    # ---------- Per-metric provenance -----------------------------------
    prov = Table(
        title="Metric provenance (Dune MCP)",
        box=box.MINIMAL,
        expand=True,
    )
    prov.add_column("Metric", style="bold")
    prov.add_column("Source")
    prov.add_column("Rows", justify="right")
    prov.add_column("Cached")
    prov.add_column("Note")

    if not metric_status:
        prov.add_row("-", "n/a", "0", "no", "Dune client unavailable")
    else:
        for name, st in metric_status.items():
            source = st.get("source", "n/a")
            available = bool(st.get("available", False))
            src_colour = (
                "green" if available else "red" if source == "error" else "yellow"
            )
            prov.add_row(
                name,
                Text(source, style=src_colour),
                str(st.get("rows", 0)),
                "yes" if st.get("cached") else "no",
                (st.get("note") or "")[:80],
            )

    # ---------- Notes ---------------------------------------------------
    grouped: list[Any] = []
    if short_circuit_note:
        grouped.append(
            Panel(
                Text(short_circuit_note, style="bold yellow"),
                border_style="yellow",
                box=box.MINIMAL,
            )
        )
    grouped += [summary, metrics, whales, vault_table, prov]
    if notes:
        notes_text = "\n".join(f"- {n}" for n in notes)
        grouped.append(
            Panel(
                Text(notes_text, style="yellow"),
                title="Notes",
                border_style="yellow",
                box=box.MINIMAL,
            )
        )

    title_suffix = (
        " (short-circuited for final decision)" if short_circuit_note else ""
    )
    return Panel(
        Group(*grouped),
        title=(
            "[bold]Level 2 - On-chain Intelligence (Dune MCP single source)"
            f"{title_suffix}[/]"
        ),
        border_style=(
            "yellow" if short_circuit_note else _colour_for_regime(regime)
        ),
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Panel: Final Decision
# ---------------------------------------------------------------------------


def final_decision_panel(decision: Any) -> Panel:
    """`decision` is a `DecisionResult` (kept duck-typed to avoid import cycles)."""
    directive = decision.directive
    action = directive.action
    regime = decision.regime
    colour = _colour_for_regime(action)

    if directive.side == "long":
        headline_emoji = "\U0001f4c8"  # 📈
    elif directive.side == "short":
        headline_emoji = "\U0001f4c9"  # 📉
    elif action == "risk_off":
        headline_emoji = "\U0001f6d1"  # 🛑
    elif action == "hold":
        headline_emoji = "\u23f8\ufe0f"  # ⏸️
    else:
        headline_emoji = "\U0001f4ca"  # 📊
    headline = Text(
        f"{headline_emoji}  FINAL DECISION  ", style="bold white on grey15"
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", justify="right")
    summary.add_column(style="white")
    summary.add_row("Final score", f"[bold]{decision.final_score:.3f}[/]")
    summary.add_row("Regime", Text(regime, style=colour))
    # Decorate the action with the side hint so SHORT opens stand out
    # at a glance ("RISK_ON (SHORT)" in red vs "RISK_ON (LONG)" in
    # green). This makes the demo unambiguous - shorts no longer look
    # like longs in the panel.
    action_label = action.upper()
    if directive.side:
        action_label = f"{action_label} ({directive.side.upper()})"
    summary.add_row(
        "Action", Text(action_label, style=_colour_for_side(directive.side) if directive.side else colour)
    )
    if directive.side:
        summary.add_row(
            "Side",
            Text(directive.side.upper(), style=_colour_for_side(directive.side)),
        )
    # Market bias is always shown - even on hold / risk-off - because
    # it's the answer to "what does on-chain say?" independent of the
    # final action the engine took.
    market_bias = getattr(directive, "market_bias", "neutral")
    bias_strength = float(getattr(directive, "bias_strength", 0.0) or 0.0)
    summary.add_row(
        "Market bias",
        Text(
            f"{market_bias.upper()}  (strength={bias_strength:.2f})",
            style=_colour_for_bias(market_bias),
        ),
    )
    summary.add_row("Intensity", f"{directive.intensity:.2f}")
    if decision.short_circuited:
        summary.add_row(
            "Short-circuit",
            f"[bold red]YES[/] - {decision.short_circuit_reason or 'L1 block'}",
        )

    weights_tbl = Table.grid(padding=(0, 2))
    weights_tbl.add_column(style="bold cyan")
    weights_tbl.add_column(style="white")
    for level in (1, 2, 3):
        s = decision.level_score(level)
        weight = decision.weights.get(f"level{level}", 0.0)
        if s is None:
            weights_tbl.add_row(f"L{level}", "-")
            continue
        weights_tbl.add_row(
            f"L{level} (w={weight:.2f})",
            f"score={s.score:.3f}  rationale={s.rationale}",
        )

    return Panel(
        Group(headline, summary, weights_tbl),
        title="[bold]Final Decision[/]",
        border_style=colour,
        box=box.HEAVY,
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Panel: Execution Plan
# ---------------------------------------------------------------------------


def execution_plan_panel(plan: Any) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column(style="white")
    table.add_row("Decision ID", str(plan.decision_id))
    # Tint the action when it explicitly opens a side; "open_short"
    # rendered in red is the unmistakable signal that the agent is
    # going short on this cycle.
    action = str(plan.action)
    action_style = "white"
    if action.startswith("open_long"):
        action_style = "bold green"
    elif action.startswith("open_short"):
        action_style = "bold red"
    elif action == "close":
        action_style = "yellow"
    elif action == "deny":
        action_style = "bold red"
    table.add_row("Action", Text(action.upper(), style=action_style))
    table.add_row("Symbol", str(plan.symbol or "-"))
    table.add_row("Size (USD)", str(plan.size_usd))
    table.add_row("Leverage", str(plan.leverage))
    # Bias / strength are now in plan.extra when the directive carried
    # them; surface them inline so the panel tells the full story.
    extra = dict(plan.extra or {})
    bias = extra.pop("market_bias", None)
    bias_strength = extra.pop("bias_strength", None)
    if bias:
        table.add_row(
            "Market bias",
            Text(
                f"{str(bias).upper()} (strength={float(bias_strength or 0):.2f})",
                style=_colour_for_bias(str(bias)),
            ),
        )
    table.add_row("Rationale", plan.rationale)
    if extra:
        table.add_row("Extra", ", ".join(f"{k}={v}" for k, v in extra.items()))
    return Panel(
        table,
        title="[bold]Execution Plan[/]",
        border_style="magenta",
        box=box.ROUNDED,
    )


# ---------------------------------------------------------------------------
# Panel: On-chain Result
# ---------------------------------------------------------------------------


def onchain_result_panel(
    tx_results: Iterable[Any],
    explorer: callable,
) -> Panel:
    table = Table(
        box=box.MINIMAL_HEAVY_HEAD,
        expand=True,
        show_header=True,
        title="Transactions",
    )
    table.add_column("Tx ID", style="bold")
    table.add_column("State")
    table.add_column("Hash")
    table.add_column("Sponsored")
    table.add_column("Explorer")

    txs = list(tx_results)
    if not txs:
        table.add_row("-", "no tx submitted", "-", "-", "-")
    else:
        for tx in txs:
            state = tx.state
            colour = (
                "green" if state in ("CONFIRMED", "COMPLETE", "COMPLETED")
                else "yellow" if state in ("PENDING", "INITIATED", "SENT", "QUEUED")
                else "blue" if state == "DRY_RUN"
                else "red"
            )
            link = explorer(tx.tx_hash) or "-"
            table.add_row(
                str(tx.tx_id),
                Text(state, style=colour),
                tx.tx_hash or "-",
                "yes" if tx.sponsored else "no",
                link,
            )
    return Panel(
        table,
        title="[bold]On-chain Result[/]",
        border_style="blue",
        box=box.ROUNDED,
    )
