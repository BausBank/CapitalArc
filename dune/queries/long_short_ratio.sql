-- CapitalArc / Level 2 / long_short_ratio  (spot-derived proxy)
-- ----------------------------------------------------------------
-- Inferred long/short ratio from spot-DEX aggressor flow over the
-- lookback window. "Long bias" = USD spent buying the asset; "short
-- bias" = asset sold for USD. We treat the ratio of those two USD
-- flows as a directional proxy for the perp L/S ratio.
--
--   ratio = sum(buy_usd) / sum(sell_usd)
--
-- The `long_account_pct` / `short_account_pct` columns are
-- approximated from the fraction of unique buyer / seller wallets,
-- not strictly the same as "% of accounts net-long" but the closest
-- thing we can compute from the spot trade tape.
--
-- Parameters
-- ----------
--   {{chain}}              text
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{sol_token_address}}  text
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol, long_short_ratio, long_account_pct, short_account_pct

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
        t.taker AS trader,
        t.amount_usd,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address THEN 'long'
            WHEN lower(CAST(t.token_sold_address   AS varchar)) = s.token_address THEN 'short'
        END AS side
    FROM dex.trades AS t
    INNER JOIN symbols AS s
        ON (
            lower(CAST(t.token_bought_address AS varchar)) = s.token_address
         OR lower(CAST(t.token_sold_address   AS varchar)) = s.token_address
        )
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
        SUM(CASE WHEN side = 'long'  THEN amount_usd END)              AS long_usd,
        SUM(CASE WHEN side = 'short' THEN amount_usd END)              AS short_usd,
        COUNT(DISTINCT CASE WHEN side = 'long'  THEN trader END)       AS long_traders,
        COUNT(DISTINCT CASE WHEN side = 'short' THEN trader END)       AS short_traders
    FROM flows
    WHERE side IS NOT NULL
    GROUP BY symbol
)
SELECT
    s.symbol                                                                       AS symbol,
    COALESCE(a.long_usd / NULLIF(a.short_usd, 0), 1.0)                             AS long_short_ratio,
    COALESCE(CAST(a.long_traders AS double)
             / NULLIF(a.long_traders + a.short_traders, 0), 0.5)                   AS long_account_pct,
    COALESCE(CAST(a.short_traders AS double)
             / NULLIF(a.long_traders + a.short_traders, 0), 0.5)                   AS short_account_pct
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
