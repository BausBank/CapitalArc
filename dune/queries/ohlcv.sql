-- CapitalArc / Level 1 / ohlcv
-- ----------------------------------------------------------------
-- Reconstructs OHLCV candles for BTC / ETH on a live, high-liquidity
-- EVM chain (Ethereum / Base / Arbitrum). Level 1 uses these candles
-- to compute EMA / RSI / ATR and apply the trend + volatility filters.
--
-- Why spot DEX trades and not perp fills?
-- ---------------------------------------
-- The Arc perp DEX is not yet indexed by Dune Analytics. Until it
-- lands in the public Dune catalog, we run Level 1 against the
-- canonical on-chain wraps of BTC / ETH on a mainstream EVM chain
-- through `dex.trades` (the multichain spot-DEX trades table). BTC
-- is filtered on WBTC / cbBTC, ETH on WETH. The agent treats these
-- candles as proxies for the perp price stream - the structural
-- signal (trend, RSI, ATR) is the same.
--
-- Parameters (ALL FIVE must be declared on the saved Dune query;
-- otherwise Dune returns HTTP 400 'unknown parameters' and the
-- DuneMCPClient has to retry without them. The Python adapter does
-- the retry transparently but it's a wasted HTTP round-trip per
-- cycle - keep these five in sync between this file and your saved
-- Dune query.)
--   {{chain}}              text   - Dune chain tag matching `dex.trades.blockchain`
--                                   (e.g. 'ethereum', 'base', 'arbitrum').
--   {{lookback_hours}}     number - window for the trade scan (typically 48).
--   {{btc_token_address}}  text   - lower-case hex of the BTC token on chain.
--   {{eth_token_address}}  text   - lower-case hex of the ETH token on chain.
--   {{min_trade_usd}}      number - min trade size in USD to keep (default 1000).
--
-- Output schema (one row per (symbol, interval, bucket_time))
-- -----------------------------------------------------------
--   symbol       text         BTC-PERP / ETH-PERP
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
