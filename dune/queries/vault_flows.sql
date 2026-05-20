-- CapitalArc / Level 2 / vault_flows
-- ----------------------------------------------------------------
-- Tracks USDC deposits / withdrawals into a watched vault (perp
-- collateral, lending pool, OTC vault - whatever the operator points
-- this at) on the configured EVM chain. Single-row result.
--
-- Until Arc is indexed by Dune, point `{{vault_address}}` at any
-- vault on Ethereum / Base / Arbitrum you want to monitor (e.g.
-- Synthetix V3 collateral on Base, GMX V2 USDC vault on Arbitrum).
-- The agent's interpretation is direction-only: net inflows lean
-- risk-on, net outflows lean risk-off.
--
-- Parameters
-- ----------
--   {{chain}}            text       - 'ethereum' / 'base' / 'arbitrum'
--   {{lookback_hours}}   number
--   {{vault_address}}    text/hex   - vault contract address (lower-case)
--   {{usdc_address}}     text/hex   - USDC token address on the chain
--
-- The schema `erc20_<chain>.evt_Transfer` is identical across the
-- supported chains; we splice {{chain}} into the schema name so the
-- same template covers every supported EVM mainnet.

WITH transfers AS (
    SELECT
        t.evt_block_time                                      AS event_time,
        lower(CAST(t.from AS varchar))                        AS from_addr,
        lower(CAST(t.to   AS varchar))                        AS to_addr,
        t.value / 1e6                                         AS amount_usdc
    FROM erc20_{{chain}}.evt_Transfer AS t
    WHERE t.contract_address = lower(CAST('{{usdc_address}}' AS varchar))
      AND t.evt_block_time
            >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
      AND (
          lower(CAST(t.to   AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
       OR lower(CAST(t.from AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
      )
),
deposits AS (
    SELECT amount_usdc FROM transfers
    WHERE to_addr = lower(CAST('{{vault_address}}' AS varchar))
),
withdrawals AS (
    SELECT amount_usdc FROM transfers
    WHERE from_addr = lower(CAST('{{vault_address}}' AS varchar))
),
all_time AS (
    -- TVL = cumulative net flow into the vault since inception
    -- (deposits minus withdrawals), expressed in USDC units.
    SELECT
        COALESCE(SUM(CASE WHEN lower(CAST(to   AS varchar))
                              = lower(CAST('{{vault_address}}' AS varchar))
                          THEN value / 1e6 ELSE 0 END), 0)
      - COALESCE(SUM(CASE WHEN lower(CAST(from AS varchar))
                              = lower(CAST('{{vault_address}}' AS varchar))
                          THEN value / 1e6 ELSE 0 END), 0)
        AS tvl_usdc
    FROM erc20_{{chain}}.evt_Transfer
    WHERE contract_address = lower(CAST('{{usdc_address}}' AS varchar))
      AND (
          lower(CAST(to   AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
       OR lower(CAST(from AS varchar)) = lower(CAST('{{vault_address}}' AS varchar))
      )
)
SELECT
    at.tvl_usdc                                                AS tvl_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM deposits),    0)    AS deposits_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM withdrawals), 0)    AS withdrawals_usdc,
    (SELECT COUNT(*) FROM deposits)                            AS deposit_events,
    (SELECT COUNT(*) FROM withdrawals)                         AS withdrawal_events,
    {{lookback_hours}}                                         AS window_hours
FROM all_time AS at;
