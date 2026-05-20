-- CapitalArc / Level 2 / volume
-- ----------------------------------------------------------------
-- Per-symbol spot-DEX volume snapshot (1h + 24h) plus last price and
-- 24h price change. Level 2 uses the (1h vs 24h-mean) ratio to detect
-- volume spikes.
--
-- Data source
-- -----------
-- `dex.trades` (Dune multichain spot DEX trades). Until Arc is
-- indexed, this is the most reliable, highest-liquidity feed for
-- BTC / ETH / SOL.
--
-- Parameters
-- ----------
--   {{chain}}              text   - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}     number - rolling window
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{sol_token_address}}  text
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol                BTC-PERP / ETH-PERP / SOL-PERP
--   last_price            most recent trade price (USD)
--   price_change_pct_24h  % change over the lookback window
--   volume_24h_usd        sum of `amount_usd` over the window
--   volume_1h_usd         sum of `amount_usd` in the latest 1h

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
    UNION ALL
    SELECT 'SOL-PERP', lower('{{sol_token_address}}')
),
window AS (
    SELECT
        s.symbol,
        t.block_time,
        t.amount_usd,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.amount_usd / NULLIF(t.token_bought_amount, 0)
            ELSE t.amount_usd / NULLIF(t.token_sold_amount, 0)
        END AS price
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
        SUM(amount_usd)                                                AS volume_24h_usd,
        SUM(CASE WHEN block_time >= NOW() - INTERVAL '1' HOUR
                 THEN amount_usd ELSE 0 END)                           AS volume_1h_usd,
        MAX_BY(price, block_time)                                      AS last_price,
        MIN_BY(price, block_time)                                      AS first_price
    FROM window
    GROUP BY symbol
)
SELECT
    s.symbol                                                           AS symbol,
    COALESCE(a.last_price, 0)                                          AS last_price,
    COALESCE(100.0 * (a.last_price - a.first_price)
             / NULLIF(a.first_price, 0), 0)                            AS price_change_pct_24h,
    COALESCE(a.volume_24h_usd, 0)                                      AS volume_24h_usd,
    COALESCE(a.volume_1h_usd, 0)                                       AS volume_1h_usd
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
