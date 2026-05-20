-- CapitalArc / Level 2 / cum_funding
-- ----------------------------------------------------------------
-- Cumulative funding paid by longs and shorts over the lookback
-- window, per symbol.
--
-- Parameters
-- ----------
--   {{chain}}            text
--   {{lookback_hours}}   number
--   {{symbols}}          text   - comma-separated list of perp symbols
--
-- Schema expectation: `{{chain}}_perp_dex.funding_settlements`
-- rows of (symbol, settle_time, side, amount_usd).

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
window AS (
    SELECT
        fs.symbol,
        fs.side,
        fs.amount_usd
    FROM {{chain}}_perp_dex.funding_settlements AS fs
    INNER JOIN symbols USING (symbol)
    WHERE fs.settle_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
)
SELECT
    s.symbol                                                              AS symbol,
    COALESCE(SUM(CASE WHEN w.side = 'long'  THEN w.amount_usd END), 0)    AS longs_paid_usd,
    COALESCE(SUM(CASE WHEN w.side = 'short' THEN w.amount_usd END), 0)    AS shorts_paid_usd,
    COALESCE(SUM(CASE WHEN w.side = 'long'  THEN w.amount_usd END), 0)
    - COALESCE(SUM(CASE WHEN w.side = 'short' THEN w.amount_usd END), 0)  AS net_flow_usd,
    {{lookback_hours}}                                                    AS window_hours
FROM symbols AS s
LEFT JOIN window AS w USING (symbol)
GROUP BY s.symbol
ORDER BY s.symbol;
