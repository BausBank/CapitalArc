-- CapitalArc / Level 2 / funding_rates
-- ----------------------------------------------------------------
-- Returns the latest funding rate snapshot for each Arc Perp DEX
-- symbol the agent trades (BTC-PERP, ETH-PERP, SOL-PERP).
--
-- Parameters
-- ----------
--   {{chain}}            text   - Dune chain tag, e.g. 'arc' / 'arc_testnet'
--   {{lookback_hours}}   number - rolling window for the 24h aggregates
--   {{symbols}}          text   - comma-separated list of perp symbols
--
-- Notes
-- -----
-- The `prices.minute` / `dex.trades`-style tables used below assume
-- Arc's perp DEX is indexed under the `{{chain}}_perp_dex` namespace
-- on Dune. Until Arc lands in the official Dune data catalog, save
-- this query with placeholder data so the agent's "metric configured"
-- branch fires and you can iterate on the rest of the pipeline.

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
events AS (
    SELECT
        ev.symbol                         AS symbol,
        ev.funding_rate                   AS rate,
        ev.funding_time                   AS funding_time,
        ev.notional_usd                   AS notional_usd
    FROM {{chain}}_perp_dex.funding_events AS ev
    INNER JOIN symbols USING (symbol)
    WHERE ev.funding_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
ranked AS (
    SELECT
        symbol,
        rate,
        funding_time,
        notional_usd,
        ROW_NUMBER() OVER (
            PARTITION BY symbol
            ORDER BY funding_time DESC
        ) AS rn
    FROM events
)
SELECT
    s.symbol                                                  AS symbol,
    MAX(CASE WHEN r.rn = 1 THEN r.rate END)                   AS current_rate,
    MAX(CASE WHEN r.rn = 1 THEN r.rate END)
        - MAX(CASE WHEN r.rn = 2 THEN r.rate END)             AS rate_8h_change,
    MAX(CASE WHEN r.rn = 1 THEN r.rate END)
        - MAX(CASE WHEN r.rn = 4 THEN r.rate END)             AS rate_24h_change,
    AVG(CASE WHEN r.rn <= 3 THEN r.rate END)                  AS weighted_average_24h,
    MAX(CASE WHEN r.rn = 1 THEN r.rate END) * 3 * 365 * 100   AS annualised_pct
FROM symbols AS s
LEFT JOIN ranked AS r USING (symbol)
GROUP BY s.symbol
ORDER BY s.symbol;
