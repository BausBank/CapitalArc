"""EntryQualityGate - the entry-quality brain for CapitalArc (Day-3).

The :class:`DecisionEngine` already separates **conviction** (how
strongly to act) from **direction** (which side) at every level and
through aggregation. What it lacked was an explicit, auditable gate
that reads BOTH axes one last time before an open is allowed - so a
borderline risk-on with a fragile directional vote, or one that only a
single level actually believes in, used to slip through.

The EntryQualityGate is that final filter. It sits AFTER
``DecisionEngine._build_directive`` on the normal cascade path and can
downgrade or block a ``risk_on`` directive. It bundles three controls:

    1. **Low-conviction filter** - refuse opens whose aggregated
       conviction or directional strength is below an operator floor.
       "Low conviction" genuinely means "stand down".

    2. **Conviction-Direction decoupler** - make the (high conviction
       but no clean side) case an EXPLICIT, logged block rather than a
       silent fall-through to hold. Reading the two axes independently
       is the whole point of the split; the gate enforces it.

    3. **Deterministic inter-level self-consistency** - measure how
       much of the (weight x conviction) mass actually AGREES with the
       final direction. When a heavy, confident level dissents (e.g.
       L2 screaming short while the weighted vote landed long), the
       open is downgraded (smaller) or blocked. Optionally folds in the
       L3 multi-sample self-consistency agreement when that feature is
       enabled upstream.

Design notes
============
* Pure logic, no side effects, no execution - it returns a verdict and
  the engine acts on it (mirrors :class:`RiskEngine` /
  :class:`PositionManager`).
* Every default is a NO-OP: the conviction / direction floors default
  to ``0.0`` and the agreement floors to ``0.0`` (with
  ``dissent_intensity_mult=1.0``), so dropping the gate into the engine
  changes nothing until the operator opts into tighter limits. This is
  the same "safe drop-in" contract the RiskEngine ships with.
* The gate is intentionally NOT applied on the L3-overrides-L1 path:
  there Claude has taken explicit authority over a rule-based veto and
  the engine already clamps intensity via the stacked-veto haircut.
  Re-filtering that decision here would double-penalise it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.utils.logging import logger

if TYPE_CHECKING:
    from src.core.decision_engine import LevelScore


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class EntryQualityConfig:
    """Tunable knobs for the entry-quality gate.

    All floors default to a NO-OP so the gate is invisible until the
    operator dials them up in ``.env``.
    """

    enabled: bool = True

    # ---- (1) Low-conviction filter ----
    # Aggregated conviction must clear this to open. 0.0 disables.
    # NOTE: kept at 0.0 by default (not RISK_ON_THRESHOLD) so the
    # mid-band STRONG-DIRECTION override - which opens at conviction in
    # (risk_off, risk_on) - is NOT silently killed. Raise to e.g. 0.6
    # to refuse every sub-threshold open.
    min_conviction_to_open: float = 0.0
    # Aggregated directional strength must clear this to open. 0.0
    # disables. Raise to e.g. 0.35 to demand a non-fragile side.
    min_direction_strength_to_open: float = 0.0

    # ---- (3) Inter-level self-consistency ----
    # Minimum fraction of the directional (weight x conviction) mass
    # that must agree with the final direction. 0.0 disables.
    min_level_agreement: float = 0.0
    # Below this agreement the open is BLOCKED outright (vs merely
    # downgraded). 0.0 means "never hard-block on dissent". Must be
    # <= min_level_agreement to be meaningful.
    block_below_agreement: float = 0.0
    # Intensity multiplier applied when a dissent DOWNGRADE fires.
    # 1.0 = no-op. 0.5 = "halve the size of a contested open".
    dissent_intensity_mult: float = 1.0
    # Minimum L3 multi-sample agreement (from the optional borderline
    # self-consistency feature). When the L3 samples disagreed more
    # than this allows, downgrade the open. 0.0 disables.
    l3_sc_min_agreement: float = 0.0


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class EntryQualityVerdict:
    """Outcome of an entry-quality evaluation.

    * ``status="pass"``      - open as-is.
    * ``status="downgrade"`` - open, but multiply intensity by
      ``intensity_mult`` (in ``(0, 1]``).
    * ``status="block"``     - do not open; the engine rewrites the
      directive to a HOLD.
    """

    status: str = "pass"  # "pass" | "downgrade" | "block"
    intensity_mult: float = 1.0
    reasons: list[str] = field(default_factory=list)
    agreement: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "block"

    @property
    def downgraded(self) -> bool:
        return self.status == "downgrade"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class EntryQualityGate:
    """Final entry-quality filter over a would-be ``risk_on`` directive.

    Construct via :meth:`from_settings` in production; tests build the
    config directly for full control over each knob.
    """

    def __init__(self, config: EntryQualityConfig | None = None) -> None:
        self.config = config or EntryQualityConfig()
        self.stats: dict[str, int] = {
            "evaluated": 0,
            "passes": 0,
            "downgrades": 0,
            "blocks": 0,
            "low_conviction_blocks": 0,
            "decoupler_blocks": 0,
            "dissent_downgrades": 0,
            "dissent_blocks": 0,
            "l3_sc_downgrades": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        conviction: float,
        direction: int,
        direction_strength: float,
        level_scores: "list[LevelScore]",
        effective_weights: dict[str, float],
        l3_self_consistency: dict[str, Any] | None = None,
    ) -> EntryQualityVerdict:
        """Evaluate a would-be open.

        Only call this for ``risk_on`` directives. The caller multiplies
        the directive's intensity by ``verdict.intensity_mult`` and, on
        ``block``, rewrites the action to ``hold``.
        """
        verdict = EntryQualityVerdict()
        if not self.config.enabled:
            return verdict

        self.stats["evaluated"] += 1
        cfg = self.config

        # ---- (1) Low-conviction filter -------------------------------
        if (
            cfg.min_conviction_to_open > 0.0
            and conviction < cfg.min_conviction_to_open
        ):
            self.stats["low_conviction_blocks"] += 1
            self.stats["blocks"] += 1
            verdict.status = "block"
            verdict.reasons.append(
                f"low_conviction: {conviction:.3f} < floor "
                f"{cfg.min_conviction_to_open:.3f}"
            )
            logger.info(
                "EntryQualityGate BLOCK | conviction {:.3f} < floor {:.3f}",
                conviction, cfg.min_conviction_to_open,
            )
            return verdict

        # ---- (2) Conviction-Direction decoupler ----------------------
        # A high-conviction read with no clean side is "stand down", not
        # "guess a direction". Make that an explicit, logged block.
        if direction == 0 or (
            cfg.min_direction_strength_to_open > 0.0
            and direction_strength < cfg.min_direction_strength_to_open
        ):
            self.stats["decoupler_blocks"] += 1
            self.stats["blocks"] += 1
            verdict.status = "block"
            verdict.reasons.append(
                f"decoupler: direction={direction:+d} "
                f"strength={direction_strength:.3f} < floor "
                f"{cfg.min_direction_strength_to_open:.3f}"
            )
            logger.info(
                "EntryQualityGate BLOCK | decoupler dir={:+d} strength "
                "{:.3f} < floor {:.3f}",
                direction, direction_strength,
                cfg.min_direction_strength_to_open,
            )
            return verdict

        # ---- (3) Inter-level self-consistency ------------------------
        agreement = self._level_agreement(
            direction, level_scores, effective_weights
        )
        verdict.agreement = agreement
        verdict.metadata["level_agreement"] = agreement

        if cfg.min_level_agreement > 0.0 and agreement < cfg.min_level_agreement:
            if (
                cfg.block_below_agreement > 0.0
                and agreement < cfg.block_below_agreement
            ):
                self.stats["dissent_blocks"] += 1
                self.stats["blocks"] += 1
                verdict.status = "block"
                verdict.reasons.append(
                    f"dissent: level agreement {agreement:.3f} < block floor "
                    f"{cfg.block_below_agreement:.3f}"
                )
                logger.info(
                    "EntryQualityGate BLOCK | level agreement {:.3f} < "
                    "block floor {:.3f}",
                    agreement, cfg.block_below_agreement,
                )
                return verdict
            self.stats["dissent_downgrades"] += 1
            verdict.status = "downgrade"
            verdict.intensity_mult = max(
                0.0, min(1.0, cfg.dissent_intensity_mult)
            )
            verdict.reasons.append(
                f"dissent: level agreement {agreement:.3f} < floor "
                f"{cfg.min_level_agreement:.3f} (intensity x"
                f"{verdict.intensity_mult:.2f})"
            )
            logger.info(
                "EntryQualityGate DOWNGRADE | level agreement {:.3f} < "
                "floor {:.3f}; intensity x{:.2f}",
                agreement, cfg.min_level_agreement, verdict.intensity_mult,
            )

        # ---- (3b) L3 multi-sample self-consistency overlay -----------
        if (
            cfg.l3_sc_min_agreement > 0.0
            and l3_self_consistency is not None
            and l3_self_consistency.get("applied")
        ):
            sc_agreement = float(
                l3_self_consistency.get("direction_agreement", 1.0) or 1.0
            )
            verdict.metadata["l3_sc_agreement"] = sc_agreement
            if sc_agreement < cfg.l3_sc_min_agreement:
                self.stats["l3_sc_downgrades"] += 1
                # Compound the downgrade (never expand intensity).
                verdict.intensity_mult = max(
                    0.0,
                    min(
                        verdict.intensity_mult,
                        cfg.dissent_intensity_mult,
                    ),
                )
                if verdict.status != "block":
                    verdict.status = "downgrade"
                verdict.reasons.append(
                    f"l3_self_consistency: sample agreement {sc_agreement:.3f} "
                    f"< floor {cfg.l3_sc_min_agreement:.3f} (intensity x"
                    f"{verdict.intensity_mult:.2f})"
                )
                logger.info(
                    "EntryQualityGate DOWNGRADE | L3 sample agreement {:.3f} "
                    "< floor {:.3f}; intensity x{:.2f}",
                    sc_agreement, cfg.l3_sc_min_agreement,
                    verdict.intensity_mult,
                )

        if verdict.status == "pass":
            self.stats["passes"] += 1
        elif verdict.status == "downgrade":
            self.stats["downgrades"] += 1
        return verdict

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _level_agreement(
        direction: int,
        level_scores: "list[LevelScore]",
        effective_weights: dict[str, float],
    ) -> float:
        """Fraction of the directional (weight x conviction) mass that
        votes the same way as ``direction``.

        Levels with no directional opinion (``direction_sign == 0``) or
        zero effective weight are ignored. When no level expresses a
        direction we return ``1.0`` (we can't measure dissent, so we
        don't manufacture it).
        """
        weights_by_level = {
            1: float(effective_weights.get("level1", 0.0) or 0.0),
            2: float(effective_weights.get("level2", 0.0) or 0.0),
            3: float(effective_weights.get("level3", 0.0) or 0.0),
        }
        agree_mass = 0.0
        total_mass = 0.0
        for s in level_scores:
            w = weights_by_level.get(s.level, 0.0)
            dsign = int(getattr(s, "direction_sign", 0) or 0)
            if w <= 0.0 or dsign == 0:
                continue
            mass = w * float(s.score)
            if mass <= 0.0:
                continue
            total_mass += mass
            if dsign == direction:
                agree_mass += mass
        if total_mass <= 1e-9:
            return 1.0
        return float(agree_mass / total_mass)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any) -> "EntryQualityGate":
        """Build an EntryQualityGate from a :class:`Settings`-like object."""

        def _get(name: str, default: Any) -> Any:
            val = getattr(settings, name, default)
            return default if val is None else val

        cfg = EntryQualityConfig(
            enabled=bool(_get("ENTRY_QUALITY_ENABLED", True)),
            min_conviction_to_open=float(
                _get("ENTRY_MIN_CONVICTION_TO_OPEN", 0.0)
            ),
            min_direction_strength_to_open=float(
                _get("ENTRY_MIN_DIRECTION_STRENGTH_TO_OPEN", 0.0)
            ),
            min_level_agreement=float(_get("ENTRY_MIN_LEVEL_AGREEMENT", 0.0)),
            block_below_agreement=float(
                _get("ENTRY_BLOCK_BELOW_AGREEMENT", 0.0)
            ),
            dissent_intensity_mult=float(
                _get("ENTRY_DISSENT_INTENSITY_MULT", 1.0)
            ),
            l3_sc_min_agreement=float(_get("L3_SC_MIN_AGREEMENT", 0.0)),
        )
        return cls(config=cfg)


__all__ = [
    "EntryQualityGate",
    "EntryQualityConfig",
    "EntryQualityVerdict",
]
