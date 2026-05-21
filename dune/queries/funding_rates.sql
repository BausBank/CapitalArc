-- CapitalArc / Level 2 / funding_rates  (spot-derived proxy)
-- ----------------------------------------------------------------
-- Spot DEXes (Uniswap / Aerodrome / Sushiswap) don't pay periodic
-- funding the way perp venues do, but they expose the same *demand
-- imbalance* signal that a funding rate captures: when one side of
-- the market is aggressively paying through, the next periodic
-- settlement on a perp would charge them. We compute that imbalance
-- directly from `dex.trades`:
--
--   imbalance(window) = (buy_usd - sell_usd) / total_usd
--
--   "buy_usd"  = trades where the asset (WBTC / WETH) was the
--                token_bought   (i.e. takers paid USD to receive it)
--   "sell_usd" = trades where the asset was the token_sold
--                (takers paid the asset to receive USD)
--
-- We then scale the imbalance into a per-8h "funding-equivalent" rate
-- using a fixed coefficient. Output schema matches the perp version
-- one-for-one so Level 2 can keep its existing extractor:
--
--   symbol, current_rate, rate_8h_change, rate_24h_change,
--   weighted_average_24h, annualised_pct
--
-- Parameters
-- ----------
--   {{chain}}              text
--   {{lookback_hours}}     number   - main window (24h typical)
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
),
flows AS (
    SELECT
        s.symbol,
        t.block_time,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.amount_usd
            ELSE -t.amount_usd
        END AS signed_usd,
        t.amount_usd AS gross_usd
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
imbalance AS (
    SELECT
        symbol,
        -- per-8h imbalance (current bucket)
        COALESCE(
            SUM(CASE WHEN block_time >= NOW() - INTERVAL '8'  HOUR THEN signed_usd END)
            / NULLIF(SUM(CASE WHEN block_time >= NOW() - INTERVAL '8' HOUR THEN gross_usd END), 0),
            0
        ) AS imb_now,
        COALESCE(
            SUM(CASE WHEN block_time >= NOW() - INTERVAL '16' HOUR
                      AND block_time <  NOW() - INTERVAL '8'  HOUR THEN signed_usd END)
            / NULLIF(SUM(CASE WHEN block_time >= NOW() - INTERVAL '16' HOUR
                                AND block_time <  NOW() - INTERVAL '8'  HOUR
                              THEN gross_usd END), 0),
            0
        ) AS imb_prev_8h,
        COALESCE(
            SUM(CASE WHEN block_time >= NOW() - INTERVAL '24' HOUR
                      AND block_time <  NOW() - INTERVAL '16' HOUR THEN signed_usd END)
            / NULLIF(SUM(CASE WHEN block_time >= NOW() - INTERVAL '24' HOUR
                                AND block_time <  NOW() - INTERVAL '16' HOUR
                              THEN gross_usd END), 0),
            0
        ) AS imb_prev_24h,
        COALESCE(
            SUM(signed_usd) / NULLIF(SUM(gross_usd), 0),
            0
        ) AS imb_24h_avg
    FROM flows
    GROUP BY symbol
)
SELECT
    s.symbol                                                              AS symbol,
    -- Map imbalance into a per-8h funding-rate scale. 1.0 of imbalance
    -- -> 0.05% per 8h (≈ 54.75% APR), which roughly matches the upper
    -- bound of CEX perp funding in extreme regimes.
    COALESCE(i.imb_now,        0) * 0.0005                                AS current_rate,
    COALESCE(i.imb_now,        0) * 0.0005
        - COALESCE(i.imb_prev_8h,  0) * 0.0005                            AS rate_8h_change,
    COALESCE(i.imb_now,        0) * 0.0005
        - COALESCE(i.imb_prev_24h, 0) * 0.0005                            AS rate_24h_change,
    COALESCE(i.imb_24h_avg,    0) * 0.0005                                AS weighted_average_24h,
    COALESCE(i.imb_now,        0) * 0.0005 * 3 * 365 * 100                AS annualised_pct
FROM symbols AS s
LEFT JOIN imbalance AS i USING (symbol)
ORDER BY s.symbol;
