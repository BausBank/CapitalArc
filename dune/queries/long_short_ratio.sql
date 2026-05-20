-- CapitalArc / Level 2 / long_short_ratio
-- ----------------------------------------------------------------
-- Returns the long/short ratio per symbol using the latest position
-- snapshot per account.
--
-- Parameters
-- ----------
--   {{chain}}      text
--   {{symbols}}    text   - comma-separated list of perp symbols
--
-- Schema expectation: `{{chain}}_perp_dex.positions_latest`
-- rows of (account, symbol, side, contracts).

WITH symbols AS (
    SELECT trim(value) AS symbol
    FROM unnest(split('{{symbols}}', ',')) AS t(value)
),
positions AS (
    SELECT
        p.symbol,
        p.side,
        p.contracts
    FROM {{chain}}_perp_dex.positions_latest AS p
    INNER JOIN symbols USING (symbol)
    WHERE p.contracts <> 0
),
agg AS (
    SELECT
        symbol,
        SUM(CASE WHEN side = 'long'  THEN contracts ELSE 0 END) AS long_qty,
        SUM(CASE WHEN side = 'short' THEN contracts ELSE 0 END) AS short_qty,
        COUNT(*) FILTER (WHERE side = 'long')                   AS long_accounts,
        COUNT(*) FILTER (WHERE side = 'short')                  AS short_accounts
    FROM positions
    GROUP BY symbol
)
SELECT
    s.symbol                                                            AS symbol,
    COALESCE(a.long_qty / NULLIF(a.short_qty, 0), 1.0)                  AS long_short_ratio,
    COALESCE(
        a.long_accounts::float / NULLIF(a.long_accounts + a.short_accounts, 0),
        0.5
    )                                                                   AS long_account_pct,
    COALESCE(
        a.short_accounts::float / NULLIF(a.long_accounts + a.short_accounts, 0),
        0.5
    )                                                                   AS short_account_pct
FROM symbols AS s
LEFT JOIN agg AS a USING (symbol)
ORDER BY s.symbol;
