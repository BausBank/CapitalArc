-- CapitalArc / Level 2 / open_interest  (spot-derived proxy)
-- ----------------------------------------------------------------
-- Spot DEXes don't expose a perp "open interest", so we proxy it
-- with the *rolling-USD trade volume* on each symbol and report
-- 1h / 4h / 24h deltas. The structural signal Level 2 cares about
-- (positive vs negative deltas) translates cleanly to perp OI.
--
-- Parameters
-- ----------
--   {{chain}}              text   - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol, current_contracts, current_value_usd,
--   delta_1h_pct, delta_4h_pct, delta_24h_pct
WITH symbols AS (
    SELECT 'BTC-PERP' AS symbol, lower('{{btc_token_address}}') AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
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
        ON lower(CAST(t.token_bought_address AS varchar)) = s.token_address
        OR lower(CAST(t.token_sold_address AS varchar)) = s.token_address
    WHERE t.blockchain = '{{chain}}'
      AND t.block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND t.amount_usd >= 500
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
agg AS (
    SELECT
        symbol,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '1' HOUR THEN asset_amount END) AS amt_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '1' HOUR THEN amount_usd END) AS usd_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '2' HOUR
                  AND block_time < NOW() - INTERVAL '1' HOUR THEN amount_usd END) AS usd_prev_1h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '4' HOUR
                  AND block_time < NOW() - INTERVAL '1' HOUR THEN amount_usd END) AS usd_prev_3h,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '24' HOUR
                  AND block_time < NOW() - INTERVAL '1' HOUR THEN amount_usd END) AS usd_prev_23h
    FROM flows
    GROUP BY symbol
)
SELECT
    s.symbol,
    COALESCE(a.amt_1h, 0) AS current_contracts,
    COALESCE(a.usd_1h, 0) AS current_value_usd,
    COALESCE(100.0 * (a.usd_1h - a.usd_prev_1h) / NULLIF(a.usd_prev_1h, 0), 0) AS delta_1h_pct,
    COALESCE(100.0 * (a.usd_1h - a.usd_prev_3h / 3.0) / NULLIF(a.usd_prev_3h / 3.0, 0), 0) AS delta_4h_pct,
    COALESCE(100.0 * (a.usd_1h - a.usd_prev_23h / 23.0) / NULLIF(a.usd_prev_23h / 23.0, 0), 0) AS delta_24h_pct
FROM symbols AS s
LEFT JOIN agg AS a ON s.symbol = a.symbol
ORDER BY s.symbol;
