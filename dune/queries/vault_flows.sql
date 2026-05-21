-- CapitalArc / Level 2 / vault_flows
-- ----------------------------------------------------------------
-- USDC deposits / withdrawals for a watched vault on {{chain}} +
-- cumulative TVL (deposits - withdrawals since vault inception).
-- Single-row result.
--
-- Until Arc is indexed by Dune, point {{vault_address}} at any vault
-- on Ethereum / Base / Arbitrum you want to monitor (e.g. Synthetix
-- V3 collateral on Base). The agent's interpretation is direction-
-- only: net inflows lean risk-on, net outflows lean risk-off.
--
-- Parameters
-- ----------
--   {{chain}}            text   - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}   number
--   {{vault_address}}    text   - vault contract address (lower-case)
--   {{usdc_address}}     text   - USDC token address on the chain
--
-- Note: erc20_{{chain}}.evt_Transfer is spliced via Dune's text
-- substitution, so the same template covers every supported chain.
-- Note: in erc20_<chain>.evt_Transfer, the columns `from`, `to` and
-- `contract_address` are all `varbinary` in Trino. Trino does not do
-- implicit `varbinary <-> varchar` casts, so every comparison wraps
-- the column in `lower(CAST(... AS varchar))` to match the lower-case
-- hex string we get from Dune's text-substituted {{...}} parameters.
WITH deposits AS (
    SELECT CAST(value AS double) / 1e6 AS amount_usdc
    FROM erc20_{{chain}}.evt_Transfer
    WHERE lower(CAST(contract_address AS varchar)) = lower(CAST('{{usdc_address}}' AS varchar))
      AND lower(CAST(to AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
      AND evt_block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
withdrawals AS (
    SELECT CAST(value AS double) / 1e6 AS amount_usdc
    FROM erc20_{{chain}}.evt_Transfer
    WHERE lower(CAST(contract_address AS varchar)) = lower(CAST('{{usdc_address}}' AS varchar))
      AND lower(CAST("from" AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
      AND evt_block_time >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
tvl AS (
    -- Cumulative net flow into the vault since inception (USDC units).
    -- `from` is reserved in Trino, hence the "from" double-quoting.
    SELECT
        COALESCE(SUM(CASE WHEN lower(CAST(to AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
                          THEN CAST(value AS double) / 1e6 ELSE 0 END), 0)
      - COALESCE(SUM(CASE WHEN lower(CAST("from" AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
                          THEN CAST(value AS double) / 1e6 ELSE 0 END), 0) AS tvl_usdc
    FROM erc20_{{chain}}.evt_Transfer
    WHERE lower(CAST(contract_address AS varchar)) = lower(CAST('{{usdc_address}}' AS varchar))
      AND (
          lower(CAST(to AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
          OR lower(CAST("from" AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
      )
)
SELECT
    COALESCE((SELECT tvl_usdc FROM tvl), 0) AS tvl_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM deposits), 0) AS deposits_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM withdrawals), 0) AS withdrawals_usdc,
    (SELECT COUNT(*) FROM deposits) AS deposit_events,
    (SELECT COUNT(*) FROM withdrawals) AS withdrawal_events,
    CAST({{lookback_hours}} AS double) AS window_hours;
