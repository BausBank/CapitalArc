-- CapitalArc / Level 2 / whale_activity
-- ----------------------------------------------------------------
-- Flags large position changes by accounts inside the lookback window.
-- Returns one row per symbol with a boolean flag and direction.
--
-- Parameters
-- ----------
--   {{chain}}            text
--   {{lookback_hours}}   number
--   {{symbols}}          text   - comma-separated list of perp symbols
--   {{whale_min_usd}}    number - threshold for a single move (default 250000)
--
-- Schema expectation: `{{chain}}_perp_dex.position_changes`
-- rows of (account, symbol, change_time, delta_contracts, mark_price).

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
moves AS (
    SELECT
        pc.symbol,
        pc.account,
        pc.delta_contracts,
        pc.mark_price,
        ABS(pc.delta_contracts) * pc.mark_price AS notional_usd
    FROM {{chain}}_perp_dex.position_changes AS pc
    INNER JOIN symbols USING (symbol)
    WHERE pc.change_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
big_moves AS (
    SELECT
        symbol,
        SUM(CASE WHEN delta_contracts > 0
                 THEN notional_usd ELSE -notional_usd END)         AS signed_notional,
        SUM(notional_usd)                                          AS gross_notional,
        COUNT(*)                                                   AS n_moves
    FROM moves
    WHERE notional_usd >= COALESCE({{whale_min_usd}}, 250000)
    GROUP BY symbol
)
SELECT
    s.symbol                                                       AS symbol,
    (bm.gross_notional IS NOT NULL)                                AS flagged,
    CASE
        WHEN bm.signed_notional > 0 THEN 'accumulating'
        WHEN bm.signed_notional < 0 THEN 'distributing'
        ELSE 'neutral'
    END                                                            AS direction,
    COALESCE(bm.signed_notional, 0)                                AS notional_usd_change,
    CONCAT(
        COALESCE(CAST(bm.n_moves AS varchar), '0'),
        ' whale move(s) totalling $',
        COALESCE(CAST(ROUND(bm.gross_notional, 0) AS varchar), '0')
    )                                                              AS rationale
FROM symbols AS s
LEFT JOIN big_moves AS bm USING (symbol)
ORDER BY s.symbol;
