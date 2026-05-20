-- CapitalArc / Level 2 / volume
-- ----------------------------------------------------------------
-- Returns last price, 24h price change, 24h volume and 1h volume per
-- symbol. Level 2 uses the (1h vs 24h) ratio to detect volume spikes.
--
-- Parameters
-- ----------
--   {{chain}}            text
--   {{lookback_hours}}   number
--   {{symbols}}          text   - comma-separated list of perp symbols
--
-- Schema expectation: `{{chain}}_perp_dex.fills` rows of
-- (symbol, fill_time, price, size, notional_usd).

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
window AS (
    SELECT
        f.symbol,
        f.fill_time,
        f.price,
        f.notional_usd
    FROM {{chain}}_perp_dex.fills AS f
    INNER JOIN symbols USING (symbol)
    WHERE f.fill_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
agg AS (
    SELECT
        symbol,
        MAX(fill_time)                                                AS last_fill_time,
        SUM(notional_usd)                                             AS volume_24h_usd,
        SUM(CASE WHEN fill_time >= NOW() - INTERVAL '1' HOUR
                 THEN notional_usd ELSE 0 END)                        AS volume_1h_usd
    FROM window
    GROUP BY symbol
),
last_price_per_symbol AS (
    SELECT DISTINCT ON (symbol)
        symbol,
        price
    FROM window
    ORDER BY symbol, fill_time DESC
),
oldest_price_per_symbol AS (
    SELECT DISTINCT ON (symbol)
        symbol,
        price
    FROM window
    ORDER BY symbol, fill_time ASC
)
SELECT
    s.symbol                                                              AS symbol,
    lp.price                                                              AS last_price,
    100.0 * (lp.price - op.price) / NULLIF(op.price, 0)                   AS price_change_pct_24h,
    a.volume_24h_usd                                                      AS volume_24h_usd,
    a.volume_1h_usd                                                       AS volume_1h_usd
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
LEFT JOIN last_price_per_symbol AS lp USING (symbol)
LEFT JOIN oldest_price_per_symbol AS op USING (symbol)
ORDER BY s.symbol;
