-- CapitalArc / Level 2 / cum_funding  (spot-derived proxy)
-- ----------------------------------------------------------------
-- Spot DEXes don't settle funding, so "cumulative funding paid" is
-- represented as the cumulative signed aggressor flow over the
-- window scaled into a notional funding cost.
--
--   net_signed_flow = sum(buy_usd) - sum(sell_usd)
--
-- A positive net flow means the market spent more USD buying the
-- asset than they received from selling it (longs would be paying
-- on a perp). We translate that net flow into a funding-equivalent
-- by applying the same 0.05% / 8h scale used in `funding_rates.sql`
-- across the number of 8h windows in the lookback.
--
-- Parameters
-- ----------
--   {{chain}}              text
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{sol_token_address}}  text
--
-- Output
-- ------
--   symbol, longs_paid_usd, shorts_paid_usd, net_flow_usd, window_hours

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
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN t.amount_usd
            ELSE 0
        END AS buy_usd,
        CASE
            WHEN lower(CAST(t.token_sold_address   AS varchar)) = s.token_address
                THEN t.amount_usd
            ELSE 0
        END AS sell_usd
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
        SUM(buy_usd)                                                AS gross_buy_usd,
        SUM(sell_usd)                                               AS gross_sell_usd
    FROM flows
    GROUP BY symbol
)
SELECT
    s.symbol                                                            AS symbol,
    -- Scale: 0.05% / 8h applied across the window. `windows = h / 8`.
    COALESCE(GREATEST(a.gross_buy_usd - a.gross_sell_usd, 0)
             * 0.0005
             * ({{lookback_hours}} / 8.0), 0)                           AS longs_paid_usd,
    COALESCE(GREATEST(a.gross_sell_usd - a.gross_buy_usd, 0)
             * 0.0005
             * ({{lookback_hours}} / 8.0), 0)                           AS shorts_paid_usd,
    COALESCE((a.gross_buy_usd - a.gross_sell_usd)
             * 0.0005
             * ({{lookback_hours}} / 8.0), 0)                           AS net_flow_usd,
    {{lookback_hours}}                                                  AS window_hours
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
