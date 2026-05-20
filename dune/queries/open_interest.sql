-- CapitalArc / Level 2 / open_interest
-- ----------------------------------------------------------------
-- Returns open-interest level and 1h / 4h / 24h deltas per symbol.
--
-- Parameters
-- ----------
--   {{chain}}            text
--   {{lookback_hours}}   number
--   {{symbols}}          text   - comma-separated list of perp symbols
--
-- Schema expectation: `{{chain}}_perp_dex.oi_snapshots` rows of
-- (symbol, snapshot_time, contracts, value_usd).

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
points AS (
    SELECT
        oi.symbol,
        oi.snapshot_time,
        oi.contracts,
        oi.value_usd,
        ROW_NUMBER() OVER (
            PARTITION BY oi.symbol
            ORDER BY oi.snapshot_time DESC
        ) AS rn
    FROM {{chain}}_perp_dex.oi_snapshots AS oi
    INNER JOIN symbols USING (symbol)
    WHERE oi.snapshot_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
)
SELECT
    s.symbol                                                AS symbol,
    MAX(CASE WHEN p.rn = 1 THEN p.contracts END)            AS current_contracts,
    MAX(CASE WHEN p.rn = 1 THEN p.value_usd END)            AS current_value_usd,
    -- We assume 1h sampling cadence; tune the WHEN bounds to match.
    100.0 * (
        MAX(CASE WHEN p.rn = 1 THEN p.contracts END)
        - MAX(CASE WHEN p.rn = 2 THEN p.contracts END)
    ) / NULLIF(MAX(CASE WHEN p.rn = 2 THEN p.contracts END), 0)
                                                            AS delta_1h_pct,
    100.0 * (
        MAX(CASE WHEN p.rn = 1 THEN p.contracts END)
        - MAX(CASE WHEN p.rn = 5 THEN p.contracts END)
    ) / NULLIF(MAX(CASE WHEN p.rn = 5 THEN p.contracts END), 0)
                                                            AS delta_4h_pct,
    100.0 * (
        MAX(CASE WHEN p.rn = 1 THEN p.contracts END)
        - MAX(CASE WHEN p.rn = 25 THEN p.contracts END)
    ) / NULLIF(MAX(CASE WHEN p.rn = 25 THEN p.contracts END), 0)
                                                            AS delta_24h_pct
FROM symbols AS s
LEFT JOIN points AS p USING (symbol)
GROUP BY s.symbol
ORDER BY s.symbol;
