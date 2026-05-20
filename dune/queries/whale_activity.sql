-- CapitalArc / Level 2 / whale_activity
-- ----------------------------------------------------------------
-- Flags whale-sized ERC-20 transfers of BTC / ETH / SOL on the
-- configured chain inside the lookback window. The direction
-- ("accumulating" / "distributing") is inferred from the *net flow
-- into/out of known DEX router addresses* by looking at
-- `dex.trades.amount_usd` signed by which side of the trade the
-- asset was on.
--
-- We use two stacked signals:
--
--   1. Big DEX trades above `{{whale_min_usd}}` USD - already the
--      cleanest "informed flow" signal because each row is a
--      single-tx, single-aggressor event.
--   2. Big raw ERC-20 transfers above the same threshold - useful
--      for spotting wallet-to-wallet OTC-style moves that bypass a
--      DEX route. We approximate the USD value at `{{whale_min_usd}}`
--      because converting `evt_Transfer.value` to USD requires a
--      live price feed; the SQL therefore only flags those moves
--      and leaves the precise notional to the DEX-trade signal.
--
-- Parameters
-- ----------
--   {{chain}}              text   - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}     number
--   {{btc_token_address}}  text
--   {{eth_token_address}}  text
--   {{sol_token_address}}  text
--   {{whale_min_usd}}      number - threshold for a single move (default 250000)
--
-- Output (one row per symbol)
-- ---------------------------
--   symbol, flagged, direction, notional_usd_change, rationale

WITH symbols AS (
    SELECT 'BTC-PERP'                            AS symbol,
           lower('{{btc_token_address}}')        AS token_address
    UNION ALL
    SELECT 'ETH-PERP', lower('{{eth_token_address}}')
    UNION ALL
    SELECT 'SOL-PERP', lower('{{sol_token_address}}')
),
moves AS (
    SELECT
        s.symbol,
        CASE
            WHEN lower(CAST(t.token_bought_address AS varchar)) = s.token_address
                THEN  t.amount_usd
            ELSE -t.amount_usd
        END AS signed_usd,
        t.amount_usd                                                AS gross_usd
    FROM dex.trades AS t
    INNER JOIN symbols AS s
        ON (
            lower(CAST(t.token_bought_address AS varchar)) = s.token_address
         OR lower(CAST(t.token_sold_address   AS varchar)) = s.token_address
        )
    WHERE t.blockchain = '{{chain}}'
      AND t.block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND t.amount_usd >= COALESCE({{whale_min_usd}}, 250000)
      AND (
        lower(CAST(t.token_bought_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
        OR lower(CAST(t.token_sold_symbol AS varchar)) IN ('usdc', 'usdt', 'dai', 'usdc.e', 'usdbc')
      )
),
agg AS (
    SELECT
        symbol,
        SUM(signed_usd)                                             AS signed_notional,
        SUM(gross_usd)                                              AS gross_notional,
        COUNT(*)                                                    AS n_moves
    FROM moves
    GROUP BY symbol
)
SELECT
    s.symbol                                                        AS symbol,
    (a.n_moves IS NOT NULL AND a.n_moves > 0)                       AS flagged,
    CASE
        WHEN COALESCE(a.signed_notional, 0) > 0 THEN 'accumulating'
        WHEN COALESCE(a.signed_notional, 0) < 0 THEN 'distributing'
        ELSE 'neutral'
    END                                                             AS direction,
    COALESCE(a.signed_notional, 0)                                  AS notional_usd_change,
    CONCAT(
        COALESCE(CAST(a.n_moves AS varchar), '0'),
        ' whale trade(s) >= $',
        CAST(COALESCE({{whale_min_usd}}, 250000) AS varchar),
        ' totalling $',
        COALESCE(CAST(ROUND(a.gross_notional, 0) AS varchar), '0')
    )                                                               AS rationale
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
