-- CapitalArc / Level 2 / open_interest  (spot-derived proxy)
-- ----------------------------------------------------------------
-- Spot DEX trades don't expose `open interest` directly. We treat
-- the *rolling-window USD volume* on a symbol as a proxy for market
-- engagement / "open interest" and report deltas across 1h / 4h /
-- 24h windows. The structural signal Level 2 cares about (positive
-- vs negative deltas; whether the market is heating up or cooling
-- down) translates cleanly across both representations.
--
-- Parameters
-- ----------
--   {{chain}}              text
--   {{lookback_hours}}     number   - typically 24h
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{sol_token_address}}  text
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol               BTC-PERP / ETH-PERP / SOL-PERP
--   current_contracts    sum of base-token volume in the last 1h
--   current_value_usd    sum of USD volume in the last 1h
--   delta_1h_pct         % change vs the prior 1h
--   delta_4h_pct         % change vs the prior 4h (avg per hour)
--   delta_24h_pct        % change vs the 24h hourly average

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
    UNION ALL
    SELECT 'SOL-PERP', lower('{{sol_token_address}}')
),
flows AS (
    SELECT
        s.symbol,
        t.block_time,
        t.amount_usd,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.token_bought_amount
            ELSE t.token_sold_amount
        END AS asset_amount
    FROM dex.trades AS t
    INNER JOIN symbols AS s
        ON (
            lower(CAST(t.token_bought_address AS varchar)) = s.token_address
         OR lower(CAST(t.token_sold_address   AS varchar)) = s.token_address
        )
    WHERE t.blockchain = '{{chain}}'
      AND t.block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND t.amount_usd >= 100
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
agg AS (
    SELECT
        symbol,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '1'  HOUR THEN asset_amount END)  AS amt_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '2'  HOUR
                  AND block_time <  NOW() - INTERVAL '1'  HOUR THEN asset_amount END)  AS amt_prev_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '1'  HOUR THEN amount_usd END)    AS usd_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '4'  HOUR
                  AND block_time <  NOW() - INTERVAL '1'  HOUR THEN amount_usd END)    AS usd_prev_3h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '24' HOUR
                  AND block_time <  NOW() - INTERVAL '1'  HOUR THEN amount_usd END)    AS usd_prev_23h
    FROM flows
    GROUP BY symbol
)
SELECT
    s.symbol                                                              AS symbol,
    COALESCE(a.amt_1h, 0)                                                 AS current_contracts,
    COALESCE(a.usd_1h, 0)                                                 AS current_value_usd,
    COALESCE(100.0 * (a.amt_1h - a.amt_prev_1h)
             / NULLIF(a.amt_prev_1h, 0), 0)                               AS delta_1h_pct,
    COALESCE(100.0 * (a.usd_1h - a.usd_prev_3h / 3.0)
             / NULLIF(a.usd_prev_3h / 3.0, 0), 0)                         AS delta_4h_pct,
    COALESCE(100.0 * (a.usd_1h - a.usd_prev_23h / 23.0)
             / NULLIF(a.usd_prev_23h / 23.0, 0), 0)                       AS delta_24h_pct
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
