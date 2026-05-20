-- CapitalArc / Level 1 / ohlcv
-- ----------------------------------------------------------------
-- Returns OHLCV candles per (symbol, interval) used by Level 1 to
-- compute EMA / RSI / ATR and the trend / volatility filters.
--
-- Parameters
-- ----------
--   {{chain}}            text   - Dune chain tag, e.g. 'arc' / 'arc_testnet'
--   {{symbols}}          text   - comma-separated list of perp symbols
--   {{intervals}}        text   - comma-separated list of timeframes;
--                                 supported in this template: 15m, 1h
--   {{lookback_hours}}   number - rolling window for bucketing fills
--
-- Output schema
-- -------------
-- The agent expects rows of the following shape (column names match
-- exactly - do not rename):
--
--   symbol       text         BTC-PERP / ETH-PERP / SOL-PERP
--   interval     text         15m / 1h / ...
--   bucket_time  timestamp    UTC bucket left-edge (close_time)
--   open         float
--   high         float
--   low          float
--   close        float
--   volume       float        sum of fill sizes in the bucket
--   fills        int          number of fills inside the bucket
--
-- Notes
-- -----
-- `{{chain}}_perp_dex.fills` is the expected fills table. While Arc
-- Testnet is being indexed by the Dune team, you may need to point
-- this at a placeholder dataset (e.g. by replacing it with a
-- `WITH fills AS (SELECT * FROM ...)` CTE) so the saved query at
-- least returns rows. The agent treats an empty result as
-- `ohlcv_unavailable` and refuses to trade.

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
intervals AS (
    SELECT trim(value) AS interval_name
    FROM unnest(split('{{intervals}}', ',')) AS t(value)
),
fills AS (
    SELECT
        f.symbol,
        f.fill_time,
        f.price,
        f.size
    FROM {{chain}}_perp_dex.fills AS f
    INNER JOIN symbols USING (symbol)
    WHERE f.fill_time
          >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
fills_15m AS (
    SELECT
        symbol,
        '15m'                                                AS interval,
        date_trunc('minute', fill_time)
            - (extract(minute FROM fill_time)::int % 15)
              * INTERVAL '1 minute'                          AS bucket_time,
        price,
        size,
        fill_time
    FROM fills
    WHERE EXISTS (SELECT 1 FROM intervals WHERE interval_name = '15m')
),
fills_1h AS (
    SELECT
        symbol,
        '1h'                                                 AS interval,
        date_trunc('hour', fill_time)                        AS bucket_time,
        price,
        size,
        fill_time
    FROM fills
    WHERE EXISTS (SELECT 1 FROM intervals WHERE interval_name = '1h')
),
candles_15m AS (
    SELECT
        symbol,
        interval,
        bucket_time,
        MIN(fill_time)                              AS first_fill_time,
        MAX(fill_time)                              AS last_fill_time,
        MAX(price)                                  AS high,
        MIN(price)                                  AS low,
        SUM(size)                                   AS volume,
        COUNT(*)                                    AS fills
    FROM fills_15m
    GROUP BY symbol, interval, bucket_time
),
candles_1h AS (
    SELECT
        symbol,
        interval,
        bucket_time,
        MIN(fill_time)                              AS first_fill_time,
        MAX(fill_time)                              AS last_fill_time,
        MAX(price)                                  AS high,
        MIN(price)                                  AS low,
        SUM(size)                                   AS volume,
        COUNT(*)                                    AS fills
    FROM fills_1h
    GROUP BY symbol, interval, bucket_time
),
opens_15m AS (
    SELECT DISTINCT ON (symbol, interval, bucket_time)
        symbol, interval, bucket_time, price AS open
    FROM fills_15m
    ORDER BY symbol, interval, bucket_time, fill_time ASC
),
closes_15m AS (
    SELECT DISTINCT ON (symbol, interval, bucket_time)
        symbol, interval, bucket_time, price AS close
    FROM fills_15m
    ORDER BY symbol, interval, bucket_time, fill_time DESC
),
opens_1h AS (
    SELECT DISTINCT ON (symbol, interval, bucket_time)
        symbol, interval, bucket_time, price AS open
    FROM fills_1h
    ORDER BY symbol, interval, bucket_time, fill_time ASC
),
closes_1h AS (
    SELECT DISTINCT ON (symbol, interval, bucket_time)
        symbol, interval, bucket_time, price AS close
    FROM fills_1h
    ORDER BY symbol, interval, bucket_time, fill_time DESC
)
SELECT
    c.symbol,
    c.interval,
    c.bucket_time,
    o.open,
    c.high,
    c.low,
    cl.close,
    c.volume,
    c.fills
FROM candles_15m AS c
JOIN opens_15m   AS o  USING (symbol, interval, bucket_time)
JOIN closes_15m  AS cl USING (symbol, interval, bucket_time)

UNION ALL

SELECT
    c.symbol,
    c.interval,
    c.bucket_time,
    o.open,
    c.high,
    c.low,
    cl.close,
    c.volume,
    c.fills
FROM candles_1h AS c
JOIN opens_1h   AS o  USING (symbol, interval, bucket_time)
JOIN closes_1h  AS cl USING (symbol, interval, bucket_time)

ORDER BY symbol, interval, bucket_time;
