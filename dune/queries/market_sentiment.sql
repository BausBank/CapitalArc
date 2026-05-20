-- CapitalArc / Level 2 / market_sentiment
-- ----------------------------------------------------------------
-- Single-row result that gives Level 2 a market-wide "heat" score
-- across the three perp symbols (BTC, ETH, SOL). When this query is
-- configured, the agent uses its `heat` directly; otherwise the
-- engine falls back to the per-symbol heuristic.
--
-- Parameters
-- ----------
--   {{chain}}            text
--   {{lookback_hours}}   number
--   {{symbols}}          text
--
-- A reasonable implementation aggregates funding sign, OI 1h delta,
-- 24h price change, long/short ratio and volume into a unit-bounded
-- score; the example below blends OI delta and 24h price change.

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
oi_recent AS (
    SELECT
        oi.symbol,
        FIRST_VALUE(oi.contracts) OVER (
            PARTITION BY oi.symbol
            ORDER BY oi.snapshot_time DESC
        ) AS current_contracts,
        FIRST_VALUE(oi.contracts) OVER (
            PARTITION BY oi.symbol
            ORDER BY oi.snapshot_time ASC
        ) AS earliest_contracts
    FROM {{chain}}_perp_dex.oi_snapshots AS oi
    INNER JOIN symbols USING (symbol)
    WHERE oi.snapshot_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
price_recent AS (
    SELECT
        f.symbol,
        FIRST_VALUE(f.price) OVER (
            PARTITION BY f.symbol
            ORDER BY f.fill_time DESC
        ) AS last_price,
        FIRST_VALUE(f.price) OVER (
            PARTITION BY f.symbol
            ORDER BY f.fill_time ASC
        ) AS first_price
    FROM {{chain}}_perp_dex.fills AS f
    INNER JOIN symbols USING (symbol)
    WHERE f.fill_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
per_symbol AS (
    SELECT
        s.symbol,
        COALESCE(
            (o.current_contracts - o.earliest_contracts)
            / NULLIF(o.earliest_contracts, 0),
            0
        )                                                              AS oi_change,
        COALESCE(
            (p.last_price - p.first_price) / NULLIF(p.first_price, 0),
            0
        )                                                              AS price_change
    FROM symbols AS s
    LEFT JOIN oi_recent    AS o USING (symbol)
    LEFT JOIN price_recent AS p USING (symbol)
)
SELECT
    LEAST(
        1,
        GREATEST(
            0,
            0.5
            + AVG(0.5 * oi_change)
            + AVG(0.5 * price_change)
        )
    )                                                                  AS heat,
    'derived'                                                          AS regime,
    'heat = 0.5 + mean(0.5*OI_delta + 0.5*price_change_24h) clipped'   AS rationale
FROM per_symbol;
