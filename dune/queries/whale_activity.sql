-- CapitalArc / Level 2 / whale_activity
-- ----------------------------------------------------------------
-- Per-symbol flag for large DEX trades above {{whale_min_usd}} USD
-- inside the lookback window. Direction ('accumulating' /
-- 'distributing' / 'neutral') comes from the net signed flow:
-- positive = whales net-bought the asset.
--
-- Parameters
-- ----------
--   {{chain}}              text
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{whale_min_usd}}      number - threshold per trade (default 250000)
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol, flagged, direction, notional_usd_change, n_whales, rationale
WITH symbols AS (
    SELECT 'BTC-PERP' AS symbol, lower('{{btc_token_address}}') AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
),
moves AS (
    SELECT
        s.symbol,
        t.amount_usd AS gross_usd,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.amount_usd
            ELSE -t.amount_usd
        END AS signed_usd
    FROM dex.trades AS t
    INNER JOIN symbols AS s
        ON lower(CAST(t.token_bought_address AS varchar)) = s.token_address
        OR lower(CAST(t.token_sold_address AS varchar)) = s.token_address
    WHERE t.blockchain = '{{chain}}'
      AND t.block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND t.amount_usd >= {{whale_min_usd}}
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
agg AS (
    SELECT
        symbol,
        COUNT(*) AS n_whales,
        SUM(gross_usd) AS gross_notional,
        SUM(signed_usd) AS signed_notional
    FROM moves
    GROUP BY symbol
)
SELECT
    s.symbol,
    COALESCE(a.n_whales, 0) > 0 AS flagged,
    CASE
        WHEN COALESCE(a.signed_notional, 0) > 0 THEN 'accumulating'
        WHEN COALESCE(a.signed_notional, 0) < 0 THEN 'distributing'
        ELSE 'neutral'
    END AS direction,
    COALESCE(a.signed_notional, 0) AS notional_usd_change,
    COALESCE(a.n_whales, 0) AS n_whales,
    CONCAT(
        COALESCE(CAST(a.n_whales AS varchar), '0'),
        ' whale trade(s) >= $',
        CAST({{whale_min_usd}} AS varchar),
        ' totalling $',
        COALESCE(CAST(ROUND(a.gross_notional) AS varchar), '0')
    ) AS rationale
FROM symbols AS s
LEFT JOIN agg AS a ON s.symbol = a.symbol
ORDER BY s.symbol;
