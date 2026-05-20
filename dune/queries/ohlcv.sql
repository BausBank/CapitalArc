-- CapitalArc / Level 1 / ohlcv
-- ----------------------------------------------------------------
-- Reconstructs OHLCV candles for BTC / ETH / SOL on a live, high
-- liquidity EVM chain (Ethereum / Base / Arbitrum). Level 1 uses
-- these candles to compute EMA / RSI / ATR and apply the trend +
-- volatility filters.
--
-- Why spot DEX trades and not perp fills?
-- ---------------------------------------
-- The Arc perp DEX is not yet indexed by Dune Analytics. Until it
-- lands in the public Dune catalog, we run Level 1 against the
-- canonical on-chain wraps of BTC / ETH / SOL on a mainstream EVM
-- chain through `dex.trades` (the multichain spot-DEX trades table).
-- BTC is filtered on WBTC / cbBTC, ETH on WETH, SOL on the
-- Wormhole-wrapped SOL token. The agent treats these candles as
-- proxies for the perp price stream - the structural signal (trend,
-- RSI, ATR) is the same.
--
-- Parameters
-- ----------
--   {{chain}}              text  - Dune chain tag matching `dex.trades.blockchain`
--                                  (e.g. 'ethereum', 'base', 'arbitrum').
--   {{intervals}}          text  - comma-separated timeframes; this template
--                                  emits the union of '15m' and '1h' rows.
--   {{lookback_hours}}     number - rolling window for bucketing trades.
--   {{btc_token_address}}  text  - lower-case hex of the BTC token on chain.
--   {{eth_token_address}}  text  - lower-case hex of the ETH token on chain.
--   {{sol_token_address}}  text  - lower-case hex of the SOL token on chain.
--   {{min_trade_usd}}      number - filter dust trades (default 1000).
--
-- Output schema (one row per (symbol, interval, bucket_time))
-- -----------------------------------------------------------
--   symbol       text         BTC-PERP / ETH-PERP / SOL-PERP
--   interval     text         15m / 1h
--   bucket_time  timestamp    UTC bucket left-edge (close_time)
--   open         double
--   high         double
--   low          double
--   close        double
--   volume       double       sum of trade sizes in the bucket (token units)
--   fills        bigint       number of trades inside the bucket
--
-- Save this query in your Dune workspace and put the resulting id
-- into DUNE_QUERY_OHLCV_ID in `.env`.

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
    UNION ALL
    SELECT 'SOL-PERP', lower('{{sol_token_address}}')
),
trades AS (
    SELECT
        s.symbol,
        t.block_time,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.amount_usd / NULLIF(t.token_bought_amount, 0)
            ELSE t.amount_usd / NULLIF(t.token_sold_amount, 0)
        END AS price,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.token_bought_amount
            ELSE t.token_sold_amount
        END AS size
    FROM dex.trades AS t
    INNER JOIN symbols AS s
        ON (
            lower(CAST(t.token_bought_address AS varchar)) = s.token_address
         OR lower(CAST(t.token_sold_address   AS varchar)) = s.token_address
        )
    WHERE t.blockchain = '{{chain}}'
      AND t.block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND t.amount_usd >= COALESCE({{min_trade_usd}}, 1000)
      -- Filter out wrap / unwrap routes - keep only pairs vs a USD
      -- denominated counter-asset so `price` is in USD.
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
trades_15m AS (
    SELECT
        symbol,
        '15m'                                                 AS interval,
        date_trunc('hour', block_time)
            + INTERVAL '15' MINUTE * floor(extract(minute FROM block_time) / 15)
                                                              AS bucket_time,
        block_time,
        price,
        size
    FROM trades
),
trades_1h AS (
    SELECT
        symbol,
        '1h'                                                  AS interval,
        date_trunc('hour', block_time)                        AS bucket_time,
        block_time,
        price,
        size
    FROM trades
),
ranked AS (
    SELECT
        symbol, interval, bucket_time, block_time, price, size,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, interval, bucket_time
            ORDER BY block_time ASC
        ) AS rn_asc,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, interval, bucket_time
            ORDER BY block_time DESC
        ) AS rn_desc
    FROM (
        SELECT * FROM trades_15m
        UNION ALL
        SELECT * FROM trades_1h
    ) AS u
)
SELECT
    symbol,
    interval,
    bucket_time,
    MAX(CASE WHEN rn_asc  = 1 THEN price END)               AS open,
    MAX(price)                                              AS high,
    MIN(price)                                              AS low,
    MAX(CASE WHEN rn_desc = 1 THEN price END)               AS close,
    SUM(size)                                               AS volume,
    COUNT(*)                                                AS fills
FROM ranked
GROUP BY symbol, interval, bucket_time
ORDER BY symbol, interval, bucket_time;
