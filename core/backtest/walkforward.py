"""Purged & embargoed walk-forward splitter (Stage 1 infra, Stage 7 reuse).

The Stage-1 baseline is a *rules* engine - nothing is fitted from data, so
there is no train step to leak into a test step yet. We still build the
splitter now because:

1. The plan's DoD demands a "walk-forward split that works without
   look-ahead / leakage" - this module + its leakage test satisfy the
   letter of that.
2. Stage 7 (triple-barrier + XGBoost meta-labeling) WILL fit parameters,
   and it must train on a purged, embargoed split or it will leak. Shipping
   the splitter now means Stage 7 inherits a tested, correct primitive.

Concepts (Lopez de Prado, *Advances in Financial ML*, ch. 7)
============================================================
* **Walk-forward**: time is carved into consecutive TEST blocks; each
  block's train set is everything *before* it (anchored / expanding) or
  the immediately preceding window (rolling).
* **Purge**: drop ``purge_bars`` at the END of the train set, adjacent to
  the test block, so a label whose horizon overlaps the test window can't
  bleed backwards into training.
* **Embargo**: drop ``embargo_bars`` at the START of the test block,
  adjacent to train, so serially-correlated features straddling the
  boundary can't leak forward.

The resulting train/test index gap is exactly ``purge_bars + embargo_bars``
bars, with NO overlap - both guaranteed by :func:`walk_forward_splits` and
asserted by ``tests/test_walkforward_split.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WalkForwardSplit:
    """One walk-forward fold.

    ``train_idx`` / ``test_idx`` are 0-based positions into the sample
    (e.g. into a backtest timeline of length ``n``). For a rules-only
    baseline ``train_idx`` is informational; the harness runs the scenario
    on ``test_idx`` with a fresh (flat) book per fold.
    """

    fold: int
    train_idx: list[int]
    test_idx: list[int]

    @property
    def n_train(self) -> int:
        return len(self.train_idx)

    @property
    def n_test(self) -> int:
        return len(self.test_idx)

    @property
    def gap_bars(self) -> int:
        """Number of bars dropped between the end of train and start of test."""
        if not self.train_idx or not self.test_idx:
            return 0
        return self.test_idx[0] - self.train_idx[-1] - 1


def walk_forward_splits(
    n_samples: int,
    *,
    n_splits: int = 5,
    embargo_bars: int = 24,
    purge_bars: int = 24,
    anchored: bool = True,
    min_train_bars: int | None = None,
) -> list[WalkForwardSplit]:
    """Build ``n_splits`` purged & embargoed walk-forward folds.

    Parameters
    ----------
    n_samples:
        Length of the sample (timeline) to split.
    n_splits:
        Number of consecutive TEST blocks. The sample is divided into
        ``n_splits + 1`` equal segments; the first segment seeds the
        initial train set, the remaining ``n_splits`` become test blocks.
    embargo_bars:
        Bars dropped at the START of each test block (forward leak guard).
        Default 24 == one ``MAX_POSITION_HOLD_HOURS`` window on the 1h tape.
    purge_bars:
        Bars dropped at the END of each train set (backward leak guard).
    anchored:
        ``True`` => expanding train (everything before the test block).
        ``False`` => rolling train (only the preceding segment).
    min_train_bars:
        If set, folds whose train set is shorter than this are skipped.

    Returns
    -------
    list[WalkForwardSplit]
        One fold per test block, in chronological order. Folds whose test
        block collapses to empty (after embargo) are skipped.
    """
    if n_samples <= 0:
        return []
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    if embargo_bars < 0 or purge_bars < 0:
        raise ValueError("embargo_bars / purge_bars must be >= 0")

    seg = n_samples // (n_splits + 1)
    if seg <= 0:
        raise ValueError(
            f"n_samples={n_samples} too small for n_splits={n_splits} "
            f"(need >= {n_splits + 1} bars)"
        )

    splits: list[WalkForwardSplit] = []
    for i in range(1, n_splits + 1):
        test_start_raw = i * seg
        test_end = (i + 1) * seg if i < n_splits else n_samples

        # purge: trim the tail of train adjacent to the test block
        train_end = max(0, test_start_raw - purge_bars)
        if anchored:
            train_start = 0
        else:
            # rolling: only the immediately-preceding segment, pre-purge
            train_start = max(0, (i - 1) * seg)
        train_idx = list(range(train_start, train_end))

        # embargo: trim the head of the test block adjacent to train
        test_start = min(test_start_raw + embargo_bars, test_end)
        test_idx = list(range(test_start, test_end))

        if not test_idx:
            continue
        if min_train_bars is not None and len(train_idx) < min_train_bars:
            continue
        splits.append(
            WalkForwardSplit(fold=i, train_idx=train_idx, test_idx=test_idx)
        )
    return splits


__all__ = ["WalkForwardSplit", "walk_forward_splits"]
