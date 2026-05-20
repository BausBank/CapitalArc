-- CapitalArc / Level 2 / vault_flows
-- ----------------------------------------------------------------
-- Returns TVL + net deposits / withdrawals for `USDCCollateralVault`
-- on the Arc Perp DEX. Single-row result.
--
-- Parameters
-- ----------
--   {{chain}}            text       - 'arc' / 'arc_testnet'
--   {{lookback_hours}}   number
--   {{vault_address}}    bytea/hex  - 0x75E4FBFBA942A82F0f5CA9663571233823A71f11
--   {{usdc_address}}     bytea/hex  - 0x3600000000000000000000000000000000000000
--
-- We rely on the ERC-20 `Transfer(address,address,uint256)` event
-- on the USDC token, filtered by `to = vault` (deposits) and
-- `from = vault` (withdrawals).

WITH transfers AS (
    SELECT
        t.evt_block_time   AS event_time,
        t.from_address     AS from_addr,
        t.to_address       AS to_addr,
        t.value / 1e6      AS amount_usdc
    FROM erc20_{{chain}}.evt_Transfer AS t
    WHERE t.contract_address = lower(CAST({{usdc_address}} AS varchar))
      AND t.evt_block_time
            >= NOW() - INTERVAL '{{lookback_hours}}' HOUR
),
deposits AS (
    SELECT amount_usdc
    FROM transfers
    WHERE to_addr = lower(CAST({{vault_address}} AS varchar))
),
withdrawals AS (
    SELECT amount_usdc
    FROM transfers
    WHERE from_addr = lower(CAST({{vault_address}} AS varchar))
),
balance AS (
    -- Sum of every deposit into the vault since genesis minus every
    -- withdrawal: this is the vault's current USDC balance.
    SELECT
        COALESCE(SUM(CASE WHEN to_addr   = lower(CAST({{vault_address}} AS varchar))
                          THEN amount_usdc ELSE 0 END), 0)
      - COALESCE(SUM(CASE WHEN from_addr = lower(CAST({{vault_address}} AS varchar))
                          THEN amount_usdc ELSE 0 END), 0)
        AS tvl_usdc
    FROM erc20_{{chain}}.evt_Transfer
    WHERE contract_address = lower(CAST({{usdc_address}} AS varchar))
      AND (
          to_addr = lower(CAST({{vault_address}} AS varchar))
          OR from_addr = lower(CAST({{vault_address}} AS varchar))
      )
)
SELECT
    b.tvl_usdc                                              AS tvl_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM deposits), 0)    AS deposits_usdc,
    COALESCE((SELECT SUM(amount_usdc) FROM withdrawals), 0) AS withdrawals_usdc,
    (SELECT COUNT(*) FROM deposits)                         AS deposit_events,
    (SELECT COUNT(*) FROM withdrawals)                      AS withdrawal_events,
    {{lookback_hours}}                                      AS window_hours
FROM balance AS b;
