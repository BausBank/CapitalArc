-- CapitalArc / Level 2 / market_sentiment
-- ----------------------------------------------------------------
-- Single-row "market heat" score in [0, 1] that aggregates the spot
-- DEX signals across both perp symbols. When this query is
-- configured, Level 2 uses its `heat` directly; otherwise the engine
-- falls back to the per-symbol heuristic baked into `Level2`.
--
-- Construction
-- ------------
--   1. For each symbol, compute the 24h *imbalance* (buy_usd - sell_usd)
--      / total_usd, in [-1, 1] (positive = buying pressure).
--   2. Compute the 24h *price change* (last_price - first_price)
--      / first_price, clipped to [-0.25, 0.25] (no single asset can
--      move the score more than ±0.25 from neutral).
--   3. heat = 0.5 + 0.25 * mean(imbalance)
--                + 0.5  * clip(mean(price_change), -0.25, 0.25)
--      clipped to [0, 1].
--
-- Parameters
-- ----------
--   {{chain}}              text   - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
),
trades AS (
    SELECT
        s.symbol,
        t.block_time,
        t.amount_usd,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address THEN t.amount_usd
            ELSE -t.amount_usd
        END AS signed_usd,
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
      AND t.amount_usd >= 500
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
per_symbol AS (
    SELECT
        symbol,
        COALESCE(SUM(signed_usd) / NULLIF(SUM(amount_usd), 0), 0)         AS imbalance,
        COALESCE(
            (MAX_BY(price, block_time) - MIN_BY(price, block_time))
            / NULLIF(MIN_BY(price, block_time), 0),
            0
        )                                                                 AS price_change
    FROM trades
    GROUP BY symbol
)
SELECT
    -- COALESCE handles the empty-window case: when per_symbol has no rows
    -- (no trades matching the filter), every AVG() returns NULL and the
    -- arithmetic collapses to NULL. We return 0.5 (neutral) so the Python
    -- layer always receives a usable number and never falls back to the
    -- heuristic purely because of an empty trade window.
    COALESCE(
        LEAST(
            1,
            GREATEST(
                0,
                0.5
                + 0.25 * AVG(imbalance)
                + 0.5  * AVG(
                    CASE
                        WHEN price_change >  0.25 THEN  0.25
                        WHEN price_change < -0.25 THEN -0.25
                        ELSE price_change
                    END
                )
            )
        ),
        0.5
    )                                                                     AS heat,
    'spot-derived'                                                        AS regime,
    'heat = 0.5 + 0.25*mean(buy-sell imbalance) + 0.5*clip(mean(24h price change), ±0.25)' AS rationale
FROM per_symbol;
