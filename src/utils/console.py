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
from typing import Any, Iterable

from rich import box
from rich.box import Box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Retro terminal aesthetic: header separator rendered as `- - -` ASCII dashes
# instead of the solid `──────` of SIMPLE_HEAD.
# Rich Box line order: top / head / head_row (separator!) / mid / row / foot_row / foot / bottom
# The separator character sits at position [1] of line 3 (head_row).
RETRO_HEAD = Box(
    "    \n"
    "    \n"
    " -  \n"
    "    \n"
    "    \n"
    "    \n"
    "    \n"
    "    \n",
    ascii=True,
)


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
    table.add_column(style="bold bright_green", justify="right")
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
            table.add_row("Arc Vault TVL", _fmt_usd(tvl))

    # ---- USYC yield-leg snapshot ----------------------------------------
    # Surfaces the yield-bearing reserve directly in the demo so the
    # operator can see "how much capital is in USYC vs in perps" at a
    # glance. Falls through silently when USYC isn't configured.
    usyc = context.get("usyc")
    if usyc is not None:
        if getattr(usyc, "configured", False):
            usyc_v = float(getattr(usyc, "usyc_value_usd", 0) or 0)
            usdc_v = float(getattr(usyc, "usdc_balance", 0) or 0)
            badge = Text("ACTIVE", style="bold green")
        else:
            usyc_v = 0.0
            usdc_v = float(getattr(usyc, "usdc_balance", 0) or 0)
            badge = Text("not configured", style="dim yellow")
        table.add_row("USYC leg", badge)
        table.add_row("USYC value", _fmt_usd(usyc_v))
        table.add_row("Free Arc USDC (wallet)", _fmt_usd(usdc_v))
    return Panel(
        table,
        title="[bold bright_green][ MARKET CONTEXT ][/]",
        border_style="bright_green",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Panel: Level 1
# ---------------------------------------------------------------------------


def level1_panel(l1_raw: dict[str, Any], score: float) -> Panel:
    passes = l1_raw.get("passes", True)
    per_symbol = l1_raw.get("per_symbol", {}) or {}
    rationale = l1_raw.get("rationale", "")

    indicators = Table(
        title="[ INDICATORS — LATEST CANDLE ]",
        box=RETRO_HEAD,
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
    summary.add_column(style="bold bright_green", justify="right")
    summary.add_column(style="white")
    verdict = "[bold green]PASS[/]" if passes else "[bold red]BLOCKED[/]"
    summary.add_row("Verdict", verdict)
    summary.add_row("Score", f"{score:.3f}")
    summary.add_row("Rationale", rationale or "-")

    reasons = l1_raw.get("reasons") or []
    reasons_table = Table(
        title="[ RULE TRIGGERS ]",
        box=RETRO_HEAD,
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
        Group(summary, Text(""), indicators, reasons_table),
        title="[bold][ L1 · TECHNICAL HARD RULES ][/]",
        border_style="bright_green" if passes else "red",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Panel: Level 2
# ---------------------------------------------------------------------------


def level2_panel(l2_raw: dict[str, Any], score: float) -> Panel:
    if l2_raw.get("skipped"):
        return Panel(
            Text("Level 2 skipped (Level 1 short-circuit).", style="yellow"),
            title="[bold yellow][ L2 · ON-CHAIN INTELLIGENCE (SKIPPED) ][/]",
            border_style="yellow",
            box=box.HEAVY,
        )

    regime = l2_raw.get("regime", "neutral")
    heat = float(l2_raw.get("market_heat", 0.5))
    cached = bool(l2_raw.get("cached", False))
    per_symbol = l2_raw.get("per_symbol", {}) or {}
    metric_status = l2_raw.get("metric_status", {}) or {}
    notes = l2_raw.get("notes") or []
    dune_healthy = bool(l2_raw.get("dune_healthy", False))

    # ---------- Market summary -----------------------------------------
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold bright_green", justify="right")
    summary.add_column(style="white")
    heat_source = l2_raw.get("heat_source", "heuristic")
    heat_src_style = "green" if heat_source.startswith("dune:") else "yellow"
    heat_src_label = heat_source if heat_source.startswith("dune:") else "heuristic (fallback)"
    summary.add_row("Regime", Text(regime, style=_colour_for_regime(regime)))
    # `score` here is the L2 *conviction* the engine votes with, not the
    # raw heat - we render both so the demo shows the symmetry fix.
    conviction = float(l2_raw.get("conviction", score))
    heat_conv = float(l2_raw.get("heat_conviction", 2.0 * abs(heat - 0.5)))
    summary.add_row(
        "Conviction (engine)",
        f"[bold]{conviction:.3f}[/]  "
        f"= max(heat_conv {heat_conv:.2f}, bias_strength)",
    )
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
    dune_status = "[green]healthy[/]" if dune_healthy else "[red]unreachable[/]"
    cache_badge = "[yellow]cached[/]" if cached else "[green]live[/]"
    summary.add_row("Dune MCP", f"{dune_status}  {cache_badge}")

    # ---------- Per-symbol metrics --------------------------------------
    metrics = Table(
        title="[ PER-SYMBOL METRICS ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    metrics.add_column("Symbol", style="bold")
    metrics.add_column("Price", justify="right")
    metrics.add_column("24h %", justify="right")
    metrics.add_column("Funding", justify="right")
    metrics.add_column("APR", justify="right")
    metrics.add_column("OI", justify="right")
    metrics.add_column("OI Δ1h", justify="right")
    metrics.add_column("OI Δ24h", justify="right")
    metrics.add_column("L/S", justify="right")
    metrics.add_column("Spike")

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
        # When the HL ring buffer hasn't yet accumulated old-enough
        # samples (typical for the first cycles after a process
        # restart), the deltas surface as a placeholder zero. Render
        # "warming" instead of a misleading "+0.00%" so the operator
        # can tell "no data yet" from "real flat OI".
        oi_delta_available = bool(oi.get("oi_delta_available", True))
        if not oi_delta_available:
            oi_1h_text = Text("warming", style="dim yellow")
            oi_24h_text = Text("warming", style="dim yellow")
        else:
            oi_1h_text = Text(_fmt_pct(oi_1h), style=oi_1h_colour)
            oi_24h_text = Text(_fmt_pct(oi_24h), style=oi_24h_colour)
        ls_ratio = float(lsr.get("long_short_ratio", 1.0))
        ls_colour = (
            "green" if ls_ratio > 1.1 else "red" if ls_ratio < 0.9 else "white"
        )
        spike_text = "YES" if vol.get("spike_detected") else "no"
        spike_colour = "bold yellow" if vol.get("spike_detected") else "dim white"

        metrics.add_row(
            sym,
            _fmt_num(float(vol.get("last_price", 0)), 2),
            Text(_fmt_pct(price_change), style=change_colour),
            Text(f"{funding_rate:+.4f}%", style=funding_colour),
            f"{float(fund.get('annualised_pct', 0)):+.1f}%",
            _fmt_usd(oi.get("current_value_usd", 0)),
            oi_1h_text,
            oi_24h_text,
            Text(f"{ls_ratio:.2f}", style=ls_colour),
            Text(spike_text, style=spike_colour),
        )

    # ---------- Whales + cumulative funding -----------------------------
    whales = Table(
        title="[ WHALE ACTIVITY & CUM FUNDING ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    whales.add_column("Symbol", style="bold")
    whales.add_column("Whale", style="yellow", justify="center")
    whales.add_column("Count", justify="right")
    whales.add_column("Direction")
    whales.add_column("Notional Δ", justify="right")
    whales.add_column("Cum funding window", justify="right")
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

    # ---------- Per-metric provenance -----------------------------------
    prov = Table(
        title="[ METRIC PROVENANCE ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    prov.add_column("Metric", style="bold")
    prov.add_column("Source")
    prov.add_column("Rows", justify="right")
    prov.add_column("Cached", justify="center")

    if not metric_status:
        prov.add_row("-", "n/a", "0", "no")
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
            )

    # ---------- Notes ---------------------------------------------------
    grouped: list[Any] = [summary, metrics, whales, prov]
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

    return Panel(
        Group(*grouped),
        title="[bold bright_green][ L2 · ON-CHAIN INTELLIGENCE ][/]",
        border_style=_colour_for_regime(regime),
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Panel: Level 3 (final arbiter — Claude Sonnet 4.6 via OpenRouter)
# ---------------------------------------------------------------------------


_LEVEL3_PANEL_TITLE = "[bold bright_green][ L3 · FINAL ARBITER — SONNET 4.6 ][/]"

# Recognised section headers in the critical-mode 5-section rationale.
# When present in the rationale text, the panel paints them in bright-
# cyan bold so the user can scan the verdict at a glance. Unknown
# headers fall through to the default white body styling.
_CRITICAL_SECTION_HEADERS: tuple[str, ...] = (
    "Market Context:",
    "Key Signals Analysis:",
    "Contradictions & Risks:",
    "Contradictions and Risks:",
    "My Independent View:",
    "Final Recommendation:",
)


def _pretty_model_name(slug: str | None) -> str:
    """Render a model slug as a short, presentation-friendly label.

    Examples
    --------
    >>> _pretty_model_name("anthropic/claude-sonnet-4.6")
    'Sonnet-4.6'
    >>> _pretty_model_name("anthropic/claude-opus-4.5")
    'Opus-4.5'
    >>> _pretty_model_name("openai/gpt-4o")
    'Gpt-4o'
    >>> _pretty_model_name(None)
    'Unknown'

    Strategy: drop the vendor prefix (``anthropic/`` etc.), strip the
    ``claude-`` family prefix for Anthropic, then title-case the
    leading hyphenated segment. Leaves the version suffix intact.
    """
    if not slug:
        return "Unknown"
    tail = slug.split("/", 1)[-1]
    for prefix in ("claude-",):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
            break
    parts = tail.split("-", 1)
    parts[0] = parts[0].capitalize()
    return "-".join(parts)


def level3_panel(l3_raw: dict[str, Any], score: float) -> Panel:
    """Render the Level 3 final-arbiter verdict (Claude via OpenRouter).

    ``l3_raw`` follows the shape stamped by :class:`Level3Arbiter`:

        {
            "provider":  "openrouter" | "synthetic",
            "model":     "anthropic/claude-sonnet-4.6" | None,
            "latency_ms": float,
            "synthetic": bool,
            "fallback":  bool,                # True when the arbiter errored
            "response":  ArbiterResponse.model_dump(),
            "raw_response": <dict>,           # only on success
            "error": <str>,                   # only on fallback
            "fallback_reason": <str>,         # only on fallback
        }

    For an L1 short-circuit the raw is just ``{"skipped": True}``.
    """
    if l3_raw.get("skipped"):
        return Panel(
            Text("Level 3 skipped (Level 1 short-circuit).", style="yellow"),
            title=_LEVEL3_PANEL_TITLE + "  [yellow]· SKIPPED[/]",
            border_style="yellow",
            box=box.HEAVY,
        )

    is_synth = bool(l3_raw.get("synthetic"))
    is_fallback = bool(l3_raw.get("fallback"))
    response = l3_raw.get("response") or {}
    model_slug = l3_raw.get("model")
    model_pretty = _pretty_model_name(model_slug)
    latency_ms = float(l3_raw.get("latency_ms", 0))
    # Active L3 persona ("critical" or "standard"). Older payloads
    # might omit the key, so default to "critical" (the current
    # default) to keep section-aware rendering on.
    mode = str(l3_raw.get("mode") or "critical").lower()

    # ---------- Header / status ---------------------------------------
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold bright_green", justify="right")
    summary.add_column(style="white")

    # Provider row: short, clean and consistent across success / fallback
    # / synthetic states. Format: `Agent Sonnet-4.6, 1234ms` for the
    # happy path; the failure modes wear a status tag in front so the
    # operator notices at a glance without losing the agent identity.
    if is_synth:
        provider_label = "[bold yellow]Synthetic[/] (OPENROUTER_API_KEY unset)"
        border = "yellow"
    elif is_fallback:
        provider_label = (
            f"[bold red]FALLBACK[/]  Agent {model_pretty}, "
            f"{latency_ms:.0f}ms"
        )
        border = "red"
    else:
        provider_label = (
            f"[bold green]Agent {model_pretty}[/], {latency_ms:.0f}ms"
        )
        border = "green"

    summary.add_row("Provider", provider_label)

    # Mode badge: "CRITICAL" (red-bold, the demanding risk-manager
    # persona) or "STANDARD" (cyan, the legacy trader-voice prompt).
    # Always visible so the operator knows which prompt produced the
    # verdict.
    mode_label = {
        "critical": Text("CRITICAL", style="bold bright_red"),
        "standard": Text("STANDARD", style="bold bright_green"),
    }.get(mode, Text(mode.upper(), style="white"))
    summary.add_row("Mode", mode_label)

    direction = str(response.get("direction", "neutral"))
    direction_label = {
        "long": Text("LONG", style="bold green"),
        "short": Text("SHORT", style="bold red"),
        "neutral": Text("NEUTRAL", style="yellow"),
    }.get(direction, Text(direction.upper(), style="white"))
    regime = str(response.get("regime", "hold"))
    summary.add_row("Direction", direction_label)
    summary.add_row(
        "Regime", Text(regime, style=_colour_for_regime(regime)),
    )
    conviction = float(response.get("conviction", score))
    intensity = float(response.get("recommended_intensity", 0.0))
    summary.add_row("Conviction", f"[bold]{conviction:.3f}[/]")
    summary.add_row("Recommended intensity", f"{intensity:.3f}")

    # ---------- Rationale (visually highlighted block) -----------------
    # The rationale is the human-readable "why" - bump it into a
    # high-visibility panel with a bright cyan border. In critical
    # mode the rationale is a 5-section structured block; we paint
    # each known section header in bold bright-cyan so the user can
    # scan the verdict at a glance without re-reading the whole
    # block. In standard mode it's a single prose paragraph, rendered
    # in bold white. Always rendered in English (system prompt
    # enforces it).
    rationale = str(response.get("rationale") or "(no rationale)").strip()
    rationale_panel = Panel(
        _format_rationale_text(rationale),
        title="[bold bright_green][ RATIONALE ][/]",
        border_style="bright_green",
        box=box.HEAVY,
        padding=(0, 1),
    )

    # ---------- Key factors --------------------------------------------
    # Defensive parsing - tolerate the various shapes Claude can leak:
    # missing key, list-of-strings, list-of-dicts, comma-separated
    # string, mixed-type list. Always emit at least one tag.
    factors = _normalise_key_factors(response.get("key_factors"))
    if factors:
        factors_table = Table(
            title="[bold bright_green][ KEY FACTORS ][/]",
            box=RETRO_HEAD,
            expand=True,
            show_header=False,
            show_lines=False,
            padding=(0, 1),
        )
        # First column = colored bullet so it doesn't read as a
        # missing-value dash; second column = the factor text in white.
        factors_table.add_column(style="bold bright_yellow", width=2, justify="center")
        factors_table.add_column(style="white")
        for f in factors:
            factors_table.add_row("•", f)
    else:
        factors_table = Table.grid()
        factors_table.add_row(Text("(no factors)", style="dim"))

    # ---------- Optional fallback / synthetic notes --------------------
    children: list[Any] = [summary, rationale_panel, factors_table]
    if is_fallback:
        err = str(l3_raw.get("error") or l3_raw.get("fallback_reason") or "")[:400]
        children.append(
            Panel(
                Text(
                    "Arbiter call failed - safely held flat. Engine fell "
                    "back to conviction=0, direction=neutral, "
                    "regime=hold.\n\n"
                    f"reason: {err}",
                    style="red",
                ),
                title="Fallback details",
                border_style="red",
                box=box.MINIMAL,
            )
        )
    if is_synth:
        children.append(
            Panel(
                Text(
                    "Level 3 is in placeholder mode. Set "
                    "OPENROUTER_API_KEY in .env to enable real Claude "
                    "Sonnet 4.6 arbitration via OpenRouter. The "
                    "placeholder's weight is redistributed to L1+L2 by "
                    "the engine.",
                    style="yellow",
                ),
                title="Synthetic L3",
                border_style="yellow",
                box=box.MINIMAL,
            )
        )

    return Panel(
        Group(*children),
        title=_LEVEL3_PANEL_TITLE,
        border_style=border,
        box=box.HEAVY,
    )


def _format_rationale_text(rationale: str) -> Text:
    """Render the L3 rationale with section-aware styling.

    Critical-mode rationales follow a 5-section template (``Market
    Context:``, ``Key Signals Analysis:``, ``Contradictions & Risks:``,
    ``My Independent View:``, ``Final Recommendation:``); we paint
    those headers in bold bright-cyan so they pop out of the block
    while the body stays in bold white. Standard-mode rationales are
    a single prose paragraph and render exactly as before — the
    helper degrades gracefully when no recognised header is present.

    Bullet lines (``- ...`` / ``* ...`` / ``• ...``) get a subtle
    yellow bullet marker for readability inside the Key Signals
    Analysis section.
    """
    text = Text(justify="left")
    lines = rationale.splitlines()
    if not lines:
        text.append("(no rationale)", style="dim white")
        return text
    for idx, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        stripped = line.lstrip()
        is_header = any(
            stripped.startswith(header)
            for header in _CRITICAL_SECTION_HEADERS
        )
        if is_header:
            # Drop the trailing colon's whitespace tail but keep the
            # header text including the colon.
            text.append(stripped, style="bold bright_cyan")
        elif stripped.startswith(("- ", "* ", "• ")):
            # Replace the raw bullet glyph with a colored unicode dot
            # so the list reads cleanly inside the panel.
            indent_len = len(line) - len(stripped)
            text.append(" " * indent_len)
            text.append("• ", style="bold bright_yellow")
            text.append(stripped[2:], style="white")
        else:
            text.append(line, style="bold white")
        if idx < len(lines) - 1:
            text.append("\n")
    return text


def _normalise_key_factors(raw: Any) -> list[str]:
    """Coerce ``response.key_factors`` into a clean ``list[str]``.

    The ``ArbiterResponse`` Pydantic validator already enforces a
    list-of-strings on the inbound side, but the panel is fed the
    serialized ``model_dump()`` and we still occasionally see odd
    shapes leak through (empty list, list of empties, comma-separated
    single string). This helper makes the panel render robust to all
    of them so a flaky Claude response never produces a wall of
    empty bullets in the demo.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        # "factor a, factor b, factor c" -> ["factor a", "factor b", "factor c"]
        return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if not isinstance(raw, (list, tuple)):
        return [str(raw).strip()] if str(raw).strip() else []
    cleaned: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            # Some models return [{"tag": "x"}, ...] or [{"factor": "x"}].
            text = str(
                item.get("tag")
                or item.get("factor")
                or item.get("name")
                or item.get("text")
                or ""
            ).strip()
        else:
            text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


# ---------------------------------------------------------------------------
# Panel: Final Decision
# ---------------------------------------------------------------------------


def final_decision_panel(decision: Any) -> Panel:
    """`decision` is a `DecisionResult` (kept duck-typed to avoid import cycles)."""
    directive = decision.directive
    action = directive.action
    regime = decision.regime
    colour = _colour_for_regime(action)

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold bright_green", justify="right")
    summary.add_column(style="white")
    summary.add_row(
        "Conviction",
        f"[bold]{decision.final_score:.3f}[/]  (= final_score)",
    )
    # Aggregated direction. Distinct from L2's market_bias because it
    # blends every level's direction vote weighted by conviction.
    final_direction = int(getattr(decision, "final_direction", 0) or 0)
    direction_strength = float(
        getattr(decision, "direction_strength", 0.0) or 0.0
    )
    dir_label = (
        "LONG" if final_direction > 0
        else "SHORT" if final_direction < 0
        else "NEUTRAL"
    )
    dir_colour = (
        "bold green" if final_direction > 0
        else "bold red" if final_direction < 0
        else "yellow"
    )
    summary.add_row(
        "Direction (aggregate)",
        Text(
            f"{dir_label}  (strength={direction_strength:.2f})",
            style=dir_colour,
        ),
    )
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
    # L2 market_bias is shown alongside the aggregate direction so the
    # user can see whether on-chain alone agrees with the cross-level
    # vote.
    market_bias = getattr(directive, "market_bias", "neutral")
    bias_strength = float(getattr(directive, "bias_strength", 0.0) or 0.0)
    summary.add_row(
        "L2 market bias",
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

    # ---- L1 override audit trail ----------------------------------------
    # Render a dedicated banner whenever the L1 block path produced a
    # noteworthy decision: an actual L3 override, a hard-block uphold,
    # or an explicit L3 decline. Operators should never see a
    # mysterious "risk_on after a block" or "risk_off after a block"
    # without context.
    override_banner: Panel | None = _l1_override_banner(decision)

    weights_tbl = Table(
        title="[ LEVEL VOTES ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    weights_tbl.add_column("Level", style="bold")
    weights_tbl.add_column("Weight", style="bright_green", justify="right")
    weights_tbl.add_column("Conviction", justify="right")
    weights_tbl.add_column("Direction", justify="center")
    weights_tbl.add_column("Rationale")

    effective = getattr(decision, "effective_weights", {}) or {}
    for level in (1, 2, 3):
        s = decision.level_score(level)
        weight = decision.weights.get(f"level{level}", 0.0)
        eff = effective.get(f"level{level}", weight)
        if s is None:
            weights_tbl.add_row(f"L{level}", "-", "-", "-", "-")
            continue
        weight_str = (
            f"{weight:.2f}"
            if abs(weight - eff) < 1e-4
            else f"{weight:.2f} -> [bold yellow]{eff:.2f}[/]"
        )
        dsign = int(getattr(s, "direction_sign", 0) or 0)
        dir_arrow = (
            Text("LONG", style="bold green") if dsign > 0
            else Text("SHORT", style="bold red") if dsign < 0
            else Text("-", style="dim")
        )
        weights_tbl.add_row(
            f"L{level}",
            weight_str,
            f"{s.score:.3f}",
            dir_arrow,
            (s.rationale or "")[:120],
        )

    body: list[Any] = [summary]
    if override_banner is not None:
        body.append(override_banner)
    body.append(weights_tbl)
    return Panel(
        Group(*body),
        title="[bold bright_green][ FINAL DECISION ][/]",
        border_style=colour,
        box=box.HEAVY,
    )


def _l1_override_banner(decision: Any) -> Panel | None:
    """Render a self-contained banner describing what happened on the
    L1-block path, if anything noteworthy did.

    Returns ``None`` when there's nothing to surface (steady-state
    cycles where L1 passed cleanly).

    Three statuses are visualised:

    * ``executed`` (orange) - L3 overrode an L1 block. Shows the
      blocked codes, L3's raw vs calibrated intensity, the
      stacked-veto cap that was applied, and Claude's conviction.
    * ``hard_block_uphold`` (red) - L1 raised a hard block; L3 was
      consulted (its rationale is logged) but the engine vetoed
      regardless. Shown only when an override was attempted.
    * ``declined`` (yellow) - L3 was given the chance to override
      and explicitly declined. Surfaces *why*.
    """
    meta = getattr(decision, "l1_override_meta", None) or {}
    if not meta:
        return None
    status = str(meta.get("status", ""))
    overridden = bool(getattr(decision, "l1_overridden_by_l3", False))
    if status == "executed" and overridden:
        return _override_executed_banner(meta)
    if status == "hard_block_uphold":
        # Only render when L3 actually attempted an override -
        # otherwise the banner is noise.
        if str(meta.get("l3_direction", "neutral")) in {"long", "short"} \
                and float(meta.get("l3_conviction", 0.0) or 0.0) > 0.0:
            return _hard_block_uphold_banner(meta)
        return None
    if status == "declined":
        # Only render when there were soft codes to consider (no
        # banner on "synthetic L3 had no chance to override").
        if int(meta.get("n_soft_blocks", 0) or 0) > 0:
            return _l3_declined_banner(meta)
        return None
    return None


def _override_executed_banner(meta: dict[str, Any]) -> Panel:
    raw_i = float(meta.get("raw_intensity", 0.0) or 0.0)
    cal_i = float(meta.get("calibrated_intensity", 0.0) or 0.0)
    cap = float(meta.get("stacked_veto_cap", 1.0) or 1.0)
    intensity_repr = (
        f"{cal_i:.2f}"
        if abs(raw_i - cal_i) < 1e-4
        else f"{raw_i:.2f} -> [bold yellow]{cal_i:.2f}[/] (cap {cap:.2f})"
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold bright_green", justify="right")
    grid.add_column(style="white")
    grid.add_row(
        "Status",
        Text("L1 OVERRIDDEN BY L3", style="bold black on orange1"),
    )
    grid.add_row("Soft blocks", str(meta.get("soft_block_codes") or "-"))
    grid.add_row(
        "L3 verdict",
        f"conv={float(meta.get('l3_conviction', 0.0) or 0.0):.2f}  "
        f"dir={str(meta.get('l3_direction', '?')).upper()}",
    )
    grid.add_row("Intensity", intensity_repr)
    snippet = str(meta.get("l3_rationale_snippet", "")).strip()
    if snippet:
        grid.add_row("Rationale", Text(snippet[:240], style="italic"))
    return Panel(
        grid,
        title="[bold yellow][ L1 OVERRIDDEN BY L3 ][/]",
        border_style="yellow",
        box=box.HEAVY,
    )


def _hard_block_uphold_banner(meta: dict[str, Any]) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold bright_green", justify="right")
    grid.add_column(style="white")
    grid.add_row(
        "Status",
        Text(
            "HARD BLOCK UPHELD - L3 OVERRIDE IGNORED",
            style="bold white on red",
        ),
    )
    grid.add_row("Hard blocks", str(meta.get("hard_block_codes") or "-"))
    grid.add_row(
        "L3 attempted",
        f"conv={float(meta.get('l3_conviction', 0.0) or 0.0):.2f}  "
        f"dir={str(meta.get('l3_direction', '?')).upper()}",
    )
    snippet = str(meta.get("l3_rationale_snippet", "")).strip()
    if snippet:
        grid.add_row("L3 said", Text(snippet[:240], style="italic dim"))
    return Panel(
        grid,
        title="[bold red][ HARD L1 BLOCK UPHELD ][/]",
        border_style="red",
        box=box.HEAVY,
    )


def _l3_declined_banner(meta: dict[str, Any]) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold bright_green", justify="right")
    grid.add_column(style="white")
    grid.add_row(
        "Status",
        Text("L3 DECLINED TO OVERRIDE L1", style="bold black on yellow"),
    )
    grid.add_row("Soft blocks", str(meta.get("soft_block_codes") or "-"))
    grid.add_row(
        "Decline reason",
        str(meta.get("decline_reason", "n/a")),
    )
    snippet = str(meta.get("l3_rationale_snippet", "")).strip()
    if snippet:
        grid.add_row("L3 said", Text(snippet[:240], style="italic dim"))
    return Panel(
        grid,
        title="[bold yellow][ L3 DECLINED OVERRIDE ][/]",
        border_style="yellow",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Panel: Execution Plan
# ---------------------------------------------------------------------------


def execution_plan_panel(plan: Any) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold bright_green", justify="right")
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
    elif action == "risk_off_rotation":
        action_style = "bold yellow"
    elif action == "deposit_margin":
        action_style = "cyan"
    elif action == "deny":
        action_style = "bold red"
    table.add_row("Action", Text(action.upper(), style=action_style))
    table.add_row("Symbol", str(plan.symbol or "-"))
    table.add_row("Size (USD)", str(plan.size_usd))
    table.add_row("Leverage", str(plan.leverage))
    # Bias / strength / conviction are stamped into plan.extra by the
    # router; surface them inline so the panel tells the full story.
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
    conviction = extra.pop("conviction", None)
    intensity = extra.pop("intensity", None)
    direction_strength = extra.pop("direction_strength", None)
    if conviction is not None:
        table.add_row(
            "Conviction",
            (
                f"{float(conviction):.3f}  | intensity={float(intensity or 0):.2f}"
                f"  | direction_strength={float(direction_strength or 0):.2f}"
            ),
        )
    usyc_active = extra.pop("usyc_active", None)
    if usyc_active is not None:
        table.add_row(
            "USYC leg",
            Text("ACTIVE", style="bold green") if usyc_active
            else Text("inactive", style="dim yellow"),
        )
    sizing = extra.pop("sizing", None)
    table.add_row("", "")
    table.add_row("Rationale", plan.rationale)
    if extra:
        table.add_row("", "")
        table.add_row("Extra", ", ".join(f"{k}={v}" for k, v in extra.items()))

    renderables: list[Any] = [table]
    if sizing:
        renderables.append(Text(""))
        renderables.append(_sizing_breakdown_table(sizing))

    return Panel(
        Group(*renderables),
        title="[bold bright_green][ EXECUTION PLAN ][/]",
        border_style="bright_green",
        box=box.HEAVY,
    )


def _rotation_legs_table(legs: list[dict[str, Any]]) -> Table:
    """Render the USYC mint / redeem legs attached to an ExecutionPlan.

    Each leg is a ``{action, amount_usd, tx_id, state, reason}`` dict;
    the table colour-codes the action (green = mint USYC, blue = redeem
    USYC, dim = skipped) so the panel tells the full multi-step story
    at a glance.
    """
    table = Table(
        title="[ CAPITAL ROTATION LEGS (USDC <-> USYC) ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    table.add_column("Leg", style="bold")
    table.add_column("Amount", style="white")
    table.add_column("State", style="white")
    table.add_column("Reason", style="dim")
    for leg in legs:
        action = str(leg.get("action", "?"))
        if action == "usyc_mint":
            action_style = "bold green"
        elif action == "usyc_redeem":
            action_style = "bold cyan"
        elif action.endswith("_skipped"):
            action_style = "dim"
        else:
            action_style = "white"
        amount = leg.get("amount_usd", "0")
        try:
            amount_str = _fmt_usd(float(amount))
        except (TypeError, ValueError):
            amount_str = str(amount)
        state = str(leg.get("state", "?"))
        state_colour = (
            "green" if state in ("CONFIRMED", "COMPLETE", "COMPLETED")
            else "yellow" if state in ("PENDING", "INITIATED", "SENT")
            else "blue" if state == "DRY_RUN"
            else "dim" if state == "SKIPPED"
            else "red"
        )
        table.add_row(
            Text(action.upper(), style=action_style),
            amount_str,
            Text(state, style=state_colour),
            (leg.get("reason") or "")[:90],
        )
    return table


def _sizing_breakdown_table(sizing: dict[str, Any]) -> Table:
    """Pretty-print the position-sizing pipeline stamped by the router."""
    table = Table(
        title="[ SIZING PIPELINE ]",
        box=RETRO_HEAD,
        expand=True,
        show_lines=False,
    )
    table.add_column("Step", style="bold bright_green", no_wrap=True, min_width=16)
    table.add_column("Value", style="white", no_wrap=True, min_width=12)
    table.add_column("Formula / Note", style="dim", no_wrap=True)

    method = str(sizing.get("method", "vol_targeted"))
    method_note = {
        "vol_targeted":      "eq * risk / (stop_mult * ATR/100)",
        "directive_override": "engine forced size",
        "fallback":          "base * (1 + intensity)",
    }.get(method, method)
    table.add_row("Method", method, method_note)

    if (atr := sizing.get("atr_pct")) is not None:
        floored = sizing.get("atr_pct_floored")
        atr_str = f"{float(atr):.3f}%"
        if floored is not None and abs(float(floored) - float(atr)) > 1e-6:
            atr_str = f"{float(atr):.3f}% → {float(floored):.3f}% (floored)"
        table.add_row("ATR%", atr_str, "primary symbol avg (L1)")

    if (eq := sizing.get("equity_usd")) is not None:
        table.add_row("Equity", _fmt_usd(eq), "account equity at decision time")

    if (vt := sizing.get("vol_target_size_usd")) is not None:
        risk = sizing.get("target_risk_pct", "?")
        mult = sizing.get("stop_atr_mult", "?")
        table.add_row("Vol-target", _fmt_usd(vt), f"eq*{risk} / ({mult}*ATR/100)")

    if (ai := sizing.get("after_intensity_size_usd")) is not None:
        intensity = float(sizing.get("intensity", 0) or 0)
        table.add_row("× Intensity", _fmt_usd(ai), f"intensity = {intensity:.3f}")

    dd = sizing.get("drawdown_pct")
    hc = sizing.get("dd_haircut")
    if dd is not None or hc is not None:
        dd_v = float(dd or 0)
        hc_v = float(hc or 1)
        dd_colour = "white" if dd_v < 3 else "yellow" if dd_v < 7 else "red"
        table.add_row(
            "× DD haircut",
            Text(f"×{hc_v:.3f}  dd={dd_v:.2f}%", style=dd_colour if hc_v < 1.0 else "white"),
            "1 − (dd/max_dd)^exp",
        )

    if (ah := sizing.get("after_haircut_size_usd")) is not None:
        table.add_row("After haircut", _fmt_usd(ah), "pre-cap")

    final = sizing.get("size_usd")
    cap_hit = bool(sizing.get("cap_hit"))
    if final is not None:
        table.add_row(
            "Final size",
            Text(_fmt_usd(final), style="bold yellow" if cap_hit else "bold green"),
            "capped at max_position_usd" if cap_hit else "—",
        )

    sm = sizing.get("short_multiplier")
    if sm is not None:
        table.add_row("Short mult", f"×{float(sm):.2f}", "asymmetric short sizing")

    return table


# ---------------------------------------------------------------------------
# Panel: Position Management
# ---------------------------------------------------------------------------


def _colour_for_pm_trigger(trigger: str) -> str:
    """Colour-code each PositionManager trigger for the panel."""
    return {
        # ------- Fast-Path exit triggers -------
        "take_profit": "bold green",
        "stop_loss": "bold red",
        "trailing_stop": "bold magenta",
        "partial_take_profit": "bold cyan",
        "side_flip": "bold yellow",
        "re_evaluation": "bold cyan",
        "vol_spike_close": "bold red",
        "vol_spike_warn": "yellow",
        "time_exit": "bold yellow",
        "daily_dd_guard": "bold red",
        # ------- Fast-Path state-only verbs ----
        "breakeven_arm": "bold cyan",
        # ------- Smart-Path verdicts -----------
        "l3_smart_close": "bold red",
        "l3_smart_partial": "bold cyan",
        "l3_smart_tighten": "bold magenta",
        "l3_smart_hold": "bold green",
        "l3_smart_raise": "bold yellow",
        # HOLD-rescue: paint extra-loudly so the operator
        # immediately sees an LLM-overriden HOLD.
        "l3_hold_rescue": "bold bright_yellow",
        # ------- Earned HOLDs (positive justification) ----------------
        # Each Fast-Path HOLD carries one of these sub-triggers as of
        # the Day-6+ "HOLD must be earned" rewrite. We paint earned
        # holds in calm tones and unearned holds in attention-yellow
        # so the operator can scan at a glance.
        "hold_trend_intact":              "green",
        "hold_ranging":                   "dim cyan",
        "hold_low_conviction_profitable": "dim green",
        "hold_no_data":                   "dim magenta",
        "hold_unearned":                  "bold yellow",
        # ------- Neutral / legacy ------------------------
        "hold": "dim white",
    }.get(trigger, "white")


def _fmt_price(p: Any) -> str:
    """Render a price-ish value compactly, with `-` for missing."""
    if p is None:
        return "-"
    try:
        value = float(p)
    except (TypeError, ValueError):
        return str(p)
    if value <= 0:
        return "-"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:.6f}"


def position_review_panel(
    review: Any,
    *,
    config: Any | None = None,
    stats: Any | None = None,
) -> Panel:
    """Render the PositionManager review as a rich panel.

    ``review`` is a :class:`src.execution.position_manager.PositionReview`
    instance (or a duck-typed equivalent with ``snapshots`` / ``actions``
    attributes). When no positions are open we still print a small
    placeholder so the panel position in the report stays consistent
    across cycles.

    ``config`` is an optional :class:`PositionManagerConfig` whose
    thresholds we surface in the panel header (so operators see
    "TP=3.0% SL=2.0% trail=1.0%" at a glance without grepping the
    .env).

    ``stats`` is an optional :class:`_LLMCallStats` instance (lives
    on ``PositionManager.stats``). When provided, the panel header
    paints a "Smart-Path usage" row showing how many LLM calls have
    fired vs been skipped this session - the operator-visible proof
    that the LLM is staying rare.
    """
    snapshots = getattr(review, "snapshots", []) or []
    actions = getattr(review, "actions", []) or []
    notes = getattr(review, "notes", []) or []

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold bright_green", justify="right")
    header.add_column(style="white")

    if config is not None:
        tp_p = float(getattr(config, "take_profit_pct", 0)) * 100
        sl_p = float(getattr(config, "stop_loss_pct", 0)) * 100
        tr_p = float(getattr(config, "trailing_stop_pct", 0)) * 100
        mc = float(getattr(config, "min_conviction_to_hold", 0))
        flip = bool(getattr(config, "auto_flip_on_side_change", False))
        header.add_row(
            "Static thresholds",
            (
                f"TP=[bold green]+{tp_p:.2f}%[/]  "
                f"SL=[bold red]-{sl_p:.2f}%[/]  "
                f"trail=[bold magenta]{tr_p:.2f}%[/]  "
                f"min_conviction=[bold cyan]{mc:.2f}[/]  "
                f"flip=[bold yellow]{'on' if flip else 'off'}[/]"
            ),
        )
        if bool(getattr(config, "use_dynamic_atr_tpsl", False)):
            sl_mult = float(getattr(config, "sl_atr_mult", 0))
            tp_mult = float(getattr(config, "tp_atr_mult", 0))
            trail_mult = float(getattr(config, "trail_atr_mult", 0))
            header.add_row(
                "ATR multipliers",
                (
                    f"SL=[bold red]{sl_mult:.2f}xATR[/]  "
                    f"TP=[bold green]{tp_mult:.2f}xATR[/]  "
                    f"trail=[bold magenta]{trail_mult:.2f}xATR[/]  "
                    f"[dim](dynamic TP/SL active)[/]"
                ),
            )
        bits: list[str] = []
        if bool(getattr(config, "enable_partial_take_profit", False)):
            ptp_mult = float(getattr(config, "partial_tp_atr_mult", 0))
            ptp_frac = float(getattr(config, "partial_tp_fraction", 0)) * 100
            bits.append(
                f"partialTP=[bold cyan]{ptp_mult:.2f}xATR @ "
                f"{ptp_frac:.0f}%[/]"
            )
        if bool(getattr(config, "enable_breakeven", False)):
            be_mult = float(getattr(config, "breakeven_trigger_atr_mult", 0))
            bits.append(
                f"BE=[bold cyan]{be_mult:.2f}xATR[/]"
            )
        if bool(getattr(config, "enable_vol_filter", False)):
            spike = float(getattr(config, "vol_spike_mult", 0))
            action = str(getattr(config, "vol_spike_action", "warn"))
            bits.append(
                f"volSpike=[bold yellow]>={spike:.2f}x->"
                f"{action}[/]"
            )
        if bool(getattr(config, "enable_time_exit", False)):
            hours = float(getattr(config, "max_position_hold_hours", 0))
            bits.append(f"timeExit=[bold yellow]{hours:.1f}h[/]")
        if bits:
            header.add_row("Fast-Path features", "  ".join(bits))
        caps = getattr(config, "atr_caps", None)
        if caps is not None:
            btc_c = float(getattr(caps, "btc_1h", 0))
            eth_c = float(getattr(caps, "eth_1h", 0))
            def_c = float(getattr(caps, "default_1h", 0))
            header.add_row(
                "Per-asset ATR caps (1h)",
                (
                    f"BTC<=[bold green]{btc_c:.2f}%[/]  "
                    f"ETH<=[bold green]{eth_c:.2f}%[/]  "
                    f"default<=[bold green]{def_c:.2f}%[/]"
                ),
            )
        if bool(getattr(config, "enable_daily_dd_guard", False)):
            limit = float(getattr(config, "daily_loss_limit_pct", 0))
            current = float(getattr(review, "daily_pnl_pct", 0.0) or 0.0)
            breached = bool(getattr(review, "daily_dd_breached", False))
            colour = "bold red" if breached else (
                "yellow" if current < -limit * 0.5 else "green"
            )
            status = " [bold red](BREACHED)[/]" if breached else ""
            header.add_row(
                "Daily DD guard",
                (
                    f"session_PnL=[{colour}]{current:+.2f}%[/]  "
                    f"limit=[bold red]-{limit:.2f}%[/]{status}"
                ),
            )
        if bool(getattr(config, "enable_smart_path", False)):
            mn = int(getattr(config, "l3_review_min_interval_minutes", 0))
            mx = int(getattr(config, "l3_review_max_interval_minutes", 0))
            override = bool(getattr(config, "l3_can_override_fast_path", False))
            aggression = str(getattr(config, "l3_aggression", "balanced"))
            aggression_colour = {
                "conservative": "bold blue",
                "balanced": "bold cyan",
                "aggressive": "bold yellow",
            }.get(aggression, "bold cyan")
            first_delay = int(getattr(config, "first_review_delay_minutes", 10))
            # Pull the hard floor in from the module to render the
            # actual effective minimum (configured shift + floor).
            from src.execution.position_manager import (
                _AGGRESSION_COOLDOWN_SHIFT,
                _HARD_COOLDOWN_FLOOR_MINUTES,
            )
            shift = _AGGRESSION_COOLDOWN_SHIFT.get(aggression, 0.0)
            effective_min = max(_HARD_COOLDOWN_FLOOR_MINUTES, mn + shift)
            header.add_row(
                "Smart Path (L3)",
                (
                    f"window=[bold cyan]{int(effective_min)}-{mx}min[/]  "
                    f"(floor=[dim]{int(_HARD_COOLDOWN_FLOOR_MINUTES)}min[/])  "
                    f"first_review_delay=[bold cyan]{first_delay}min[/]  "
                    f"override_fast=[bold yellow]"
                    f"{'on' if override else 'off'}[/]  "
                    f"aggression=[{aggression_colour}]{aggression.upper()}[/]"
                ),
            )
        if bool(getattr(config, "enable_hold_rescue", False)):
            threshold = float(getattr(config, "hold_rescue_l2_min", 0.65))
            mode = str(getattr(config, "hold_rescue_direction_mode", "opposite"))
            cool = int(getattr(config, "hold_rescue_cooldown_minutes", 20))
            mode_colour = "bold yellow" if mode == "any" else "bold cyan"
            header.add_row(
                "HOLD-rescue",
                (
                    f"fires when L2 conviction >= "
                    f"[bold yellow]{threshold:.2f}[/] in "
                    f"[{mode_colour}]{mode.upper()}[/] direction; "
                    f"cooldown=[bold cyan]{cool}min[/] "
                    f"(auto-rewrites HOLD reply to partial close "
                    f"under [bold yellow]AGGRESSIVE[/] mode)"
                ),
            )
        if bool(getattr(config, "hold_must_be_earned", False)):
            mins = int(getattr(config, "hold_must_be_earned_minutes", 45))
            header.add_row(
                "HOLD-must-be-earned",
                (
                    f"re-audit positions after "
                    f"[bold cyan]{mins}min[/] of continuous HOLD "
                    f"(suppressed when previous L3 verdict was also HOLD)"
                ),
            )
        # Global LLM budget - the hard cap on AGGREGATE call volume.
        budget_per_cycle = int(getattr(config, "llm_max_per_cycle", 0))
        budget_per_hour = int(getattr(config, "llm_max_per_hour", 0))
        if budget_per_cycle > 0 or budget_per_hour > 0:
            per_cycle_txt = (
                f"[bold cyan]{budget_per_cycle}[/]/cycle"
                if budget_per_cycle > 0
                else "[dim]unbounded[/]/cycle"
            )
            per_hour_txt = (
                f"[bold cyan]{budget_per_hour}[/]/hr"
                if budget_per_hour > 0
                else "[dim]unbounded[/]/hr"
            )
            header.add_row(
                "Global LLM budget",
                (
                    f"max {per_cycle_txt}  +  max {per_hour_txt}  "
                    f"[dim](hard caps on Smart-Path call volume; "
                    f"highest-priority gates win when budget exhausted)[/]"
                ),
            )
        # Conservative-mode hardening (only relevant when conservative).
        if str(getattr(config, "l3_aggression", "")) == "conservative":
            veto_only = bool(getattr(config, "l3_conservative_veto_only", False))
            multi_trig = bool(getattr(config, "l3_require_multi_trigger", False))
            veto_txt = (
                "[bold green]ON[/]" if veto_only else "[dim]off[/]"
            )
            multi_txt = (
                "[bold green]ON[/]" if multi_trig else "[dim]off[/]"
            )
            header.add_row(
                "Conservative hardening",
                (
                    f"veto-only={veto_txt}  "
                    f"multi-trigger-AND={multi_txt}  "
                    f"[dim](LLM is a brake never an accelerator; "
                    f">=2 corroborating gates required to invoke)[/]"
                ),
            )

    if stats is not None:
        cycles = int(getattr(stats, "cycles_with_positions", 0))
        invs = int(getattr(stats, "smart_invocations", 0))
        rate = float(getattr(stats, "llm_call_rate", 0.0))
        rescue_fired = int(getattr(stats, "hold_rescues_fired", 0))
        rescue_supp = int(getattr(stats, "hold_rescues_suppressed", 0))
        h_def = int(getattr(stats, "holds_default", 0))
        h_del = int(getattr(stats, "holds_deliberate", 0))
        h_over = int(getattr(stats, "holds_rescue_overridden", 0))
        skipped_cool = int(getattr(stats, "smart_skipped_cooldown", 0))
        skipped_no_gate = int(getattr(stats, "smart_skipped_no_gate", 0))
        skipped_first = int(getattr(stats, "smart_skipped_first_review_delay", 0))
        # Day-6+ polish: new skip buckets surfaced inline so the
        # operator can attribute each "missed" LLM call to a
        # specific safety rail.
        skipped_per_cycle = int(getattr(stats, "smart_skipped_per_cycle_budget", 0))
        skipped_global = int(getattr(stats, "smart_skipped_global_budget", 0))
        skipped_multi = int(getattr(stats, "smart_skipped_conservative_multi_trigger", 0))
        blocked_veto = int(getattr(stats, "smart_blocked_positive_override", 0))
        # HOLD justification (HOLD must be earned).
        h_unearned = int(getattr(stats, "holds_unearned", 0))
        h_trend = int(getattr(stats, "holds_trend_intact", 0))
        h_ranging = int(getattr(stats, "holds_ranging", 0))
        h_lcp = int(getattr(stats, "holds_low_conviction_profitable", 0))
        h_nodata = int(getattr(stats, "holds_no_data", 0))
        # Colour the rate: green < 0.20, yellow 0.20-0.50, red >= 0.50.
        rate_colour = (
            "bold green" if rate < 0.20
            else "bold yellow" if rate < 0.50
            else "bold red"
        )
        header.add_row(
            "Smart-Path usage",
            (
                f"calls=[bold cyan]{invs}[/] in [bold cyan]{cycles}[/] "
                f"cycles ([{rate_colour}]{rate * 100:.1f}%[/]); "
                f"skipped: cooldown=[dim]{skipped_cool}[/]  "
                f"no_gate=[dim]{skipped_no_gate}[/]  "
                f"first_delay=[dim]{skipped_first}[/]  "
                f"per_cycle_budget=[dim]{skipped_per_cycle}[/]  "
                f"hourly_budget=[bold yellow]{skipped_global}[/]  "
                f"multi_trig=[dim]{skipped_multi}[/]  "
                f"veto_only_blocked=[bold yellow]{blocked_veto}[/]"
            ),
        )
        # HOLD-must-be-earned justification taxonomy. Painted with
        # the colour scheme of the per-trigger colour helper so the
        # row visually matches the per-position rows in the table.
        total_holds = h_trend + h_ranging + h_lcp + h_nodata + h_unearned
        if total_holds > 0:
            unearned_pct = h_unearned / total_holds * 100
            unearned_colour = (
                "bold red" if unearned_pct >= 30
                else "bold yellow" if unearned_pct >= 10
                else "dim"
            )
        else:
            unearned_pct = 0.0
            unearned_colour = "dim"
        header.add_row(
            "HOLD justification",
            (
                f"trend_intact=[green]{h_trend}[/]  "
                f"ranging=[dim cyan]{h_ranging}[/]  "
                f"low_conv_profit=[dim green]{h_lcp}[/]  "
                f"no_data=[dim magenta]{h_nodata}[/]  "
                f"[{unearned_colour}]unearned={h_unearned} "
                f"({unearned_pct:.0f}%)[/]"
            ),
        )
        header.add_row(
            "HOLD outcome taxonomy",
            (
                f"default=[bold cyan]{h_def}[/]  "
                f"deliberate=[bold green]{h_del}[/]  "
                f"rescue-overridden=[bold yellow]{h_over}[/]  "
                f"| rescues fired=[bold yellow]{rescue_fired}[/] "
                f"suppressed=[dim]{rescue_supp}[/]"
            ),
        )

    header.add_row("Open positions", str(len(snapshots)))
    triggered = [a for a in actions if getattr(a, "action", "hold") != "hold"]
    if triggered:
        trigger_summary = ", ".join(
            f"{a.symbol}/{a.side.upper()}:{a.trigger}" for a in triggered
        )
        header.add_row(
            "Triggers",
            Text(trigger_summary, style="bold yellow"),
        )

    renderables: list[Any] = [header]

    if snapshots:
        table = Table(
            title="[ OPEN POSITIONS ]",
            box=RETRO_HEAD,
            expand=True,
            show_lines=False,
        )
        table.add_column("Symbol", style="bold")
        table.add_column("Side")
        table.add_column("Size", justify="right")
        table.add_column("Mark", justify="right")
        table.add_column("PnL", justify="right")
        table.add_column("PnL %", justify="right")
        table.add_column("Peak %", justify="right")
        table.add_column("ATR%", justify="right")
        table.add_column("Age", justify="right")
        table.add_column("TP", justify="right")
        table.add_column("SL", justify="right")
        table.add_column("Trail", justify="right")
        table.add_column("Flags")
        table.add_column("Status", style="bold")
        for snap in snapshots:
            side = str(getattr(snap, "side", "?")).lower()
            side_style = (
                "bold green" if side == "long"
                else "bold red" if side == "short"
                else "white"
            )
            pnl_usd = getattr(snap, "unrealized_pnl_usd", Decimal("0"))
            pnl_pct = float(getattr(snap, "pnl_pct", 0.0))
            pnl_style = (
                "bold green" if pnl_pct > 0
                else "bold red" if pnl_pct < 0
                else "white"
            )
            trigger = str(getattr(snap, "trigger", "hold"))
            verb = str(getattr(snap, "action", "hold"))
            verb_label = {
                "hold": "HOLD",
                "close": "CLOSE",
                "partial_close": "PARTIAL",
                "arm_breakeven": "BE-ARM",
                "tighten_stop": "TIGHTEN",
            }.get(verb, verb.upper())
            # HOLD-must-be-earned: paint the justification sub-trigger
            # on the HOLD row so the operator sees WHY the position is
            # being held. A bare "hold" (legacy) renders as plain
            # "HOLD"; the new sub-triggers render as "HOLD trend",
            # "HOLD ranging", "HOLD profit", "HOLD nodata" or
            # "HOLD UNEARNED" (the last in bold yellow via the
            # trigger colour helper).
            if verb == "hold":
                hold_short = {
                    "hold": "HOLD",
                    "hold_trend_intact": "HOLD trend",
                    "hold_ranging": "HOLD ranging",
                    "hold_low_conviction_profitable": "HOLD profit",
                    "hold_no_data": "HOLD nodata",
                    "hold_unearned": "HOLD UNEARNED",
                }.get(trigger, "HOLD")
                action_text = hold_short
            else:
                action_text = f"{verb_label} ({trigger})"
            peak = getattr(snap, "peak_pnl_pct", None)
            peak_str = (
                f"{float(peak) * 100:+.2f}%" if peak is not None else "-"
            )
            # ATR% column (live vs cap)
            current_atr = getattr(snap, "current_atr_pct", None)
            atr_cap = getattr(snap, "atr_cap_pct", None)
            atr_breached = bool(getattr(snap, "atr_cap_breached", False))
            if current_atr is None:
                atr_text: Any = "-"
            else:
                atr_colour = (
                    "bold red" if atr_breached
                    else "yellow" if atr_cap and current_atr > atr_cap * 0.8
                    else "green"
                )
                cap_str = (
                    f"/{atr_cap:.2f}" if atr_cap is not None else ""
                )
                atr_text = Text(
                    f"{current_atr:.2f}{cap_str}%", style=atr_colour
                )
            # Age column
            age_min = getattr(snap, "age_minutes", None)
            if age_min is None:
                age_text = "-"
            elif age_min < 60:
                age_text = f"{age_min:.0f}m"
            elif age_min < 60 * 24:
                age_text = f"{age_min / 60:.1f}h"
            else:
                age_text = f"{age_min / 60 / 24:.1f}d"
            # Flags column: BE / partial / smart-path
            flag_parts: list[str] = []
            if bool(getattr(snap, "breakeven_armed", False)):
                flag_parts.append("[bold cyan]BE[/]")
            if bool(getattr(snap, "partial_tp_done", False)):
                flag_parts.append("[bold cyan]pTP[/]")
            if bool(getattr(snap, "smart_path_invoked", False)):
                verdict = str(
                    getattr(snap, "smart_path_verdict", "") or ""
                ).upper()
                short = {
                    "HOLD": "L3:hold",
                    "CLOSE_FULL": "L3:close",
                    "CLOSE_PARTIAL": "L3:partial",
                    "TIGHTEN_STOP": "L3:tighten",
                    "RAISE_TARGET": "L3:raise",
                }.get(verdict, f"L3:{verdict.lower()}")
                colour = (
                    "bold green" if verdict == "HOLD"
                    else "bold red" if "CLOSE" in verdict
                    else "bold magenta"
                )
                flag_parts.append(f"[{colour}]{short}[/]")
            flags_text = " ".join(flag_parts) if flag_parts else "-"
            table.add_row(
                str(getattr(snap, "symbol", "-")),
                Text(side.upper(), style=side_style),
                _fmt_usd(getattr(snap, "size_usd", 0)),
                _fmt_price(getattr(snap, "mark_price", None)),
                Text(_fmt_usd(pnl_usd), style=pnl_style),
                Text(f"{pnl_pct * 100:+.2f}%", style=pnl_style),
                peak_str,
                atr_text,
                age_text,
                _fmt_price(getattr(snap, "take_profit_price", None)),
                _fmt_price(getattr(snap, "stop_loss_price", None)),
                _fmt_price(getattr(snap, "trailing_stop_price", None)),
                flags_text,
                Text(action_text, style=_colour_for_pm_trigger(trigger)),
            )
        renderables.append(table)

        # Per-row reason rationale below the table, so each closing
        # trigger gets the operator a one-line "why" without truncating
        # in the main table.
        triggered_with_reason = [
            s for s in snapshots if str(getattr(s, "action", "hold")) != "hold"
        ]
        if triggered_with_reason:
            reasons = Table.grid(padding=(0, 2))
            reasons.add_column(style="bold bright_green", justify="right")
            reasons.add_column(style="white")
            for s in triggered_with_reason:
                trig = str(getattr(s, "trigger", "?"))
                source = str(getattr(s, "source", "fast")).upper()
                reasons.add_row(
                    f"{s.symbol}/{s.side.upper()}",
                    Text(
                        f"[{source}] {trig.upper()}: "
                        f"{getattr(s, 'reason', '-')}",
                        style=_colour_for_pm_trigger(trig),
                    ),
                )
                # Smart-path rationale snippet on its own dim row.
                snip = getattr(s, "smart_path_rationale", None) or getattr(
                    s, "smart_path_rationale", None
                )
                if snip:
                    reasons.add_row(
                        "  L3 says",
                        Text(str(snip), style="dim italic"),
                    )
            renderables.append(reasons)
    if notes:
        notes_table = Table.grid(padding=(0, 2))
        notes_table.add_column(style="bold bright_green", justify="right")
        notes_table.add_column(style="white")
        for note in notes:
            notes_table.add_row("note", str(note))
        renderables.append(notes_table)
    elif not snapshots:
        empty_tbl = Table.grid(padding=(0, 2))
        empty_tbl.add_column(style="bold bright_green", justify="right")
        empty_tbl.add_column(style="dim white")
        empty_tbl.add_row("Status", "no open positions")
        renderables.append(empty_tbl)

    return Panel(
        Group(*renderables),
        title="[bold bright_green][ POSITION MANAGEMENT ][/]",
        border_style="bright_green",
        box=box.HEAVY,
    )


# ---------------------------------------------------------------------------
# Panel: On-chain Result
# ---------------------------------------------------------------------------


def onchain_result_panel(
    tx_results: Iterable[Any],
    explorer: callable,
) -> Panel:
    table = Table(
        box=RETRO_HEAD,
        expand=True,
        show_header=True,
        show_lines=False,
        title="[ TRANSACTIONS ]",
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
        title="[bold bright_green][ ON-CHAIN RESULT ][/]",
        border_style="bright_green",
        box=box.HEAVY,
    )
