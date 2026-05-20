# Dune queries for CapitalArc

CapitalArc sources **all market analysis** (Level 1 OHLCV + Level 2
on-chain intelligence) **exclusively** from Dune MCP. The agent
queries a set of saved Dune queries by id; this folder ships the SQL
templates you save into your own Dune workspace.

Arc RPC is **not** used for market data; it only serves account state
(agent wallet balance, vault TVL/margin reads in the Market Context
panel). Every candle, every funding-proxy, every whale-flow signal -
all of it comes from Dune.

## Why Ethereum / Base / Arbitrum (and not Arc Testnet)?

The Arc Testnet is not yet indexed by the public Dune catalog (no
`arc.dex.trades`, no `erc20_arc.evt_Transfer`). Until it lands there,
CapitalArc points its decision engine at a live, high-liquidity EVM
chain via Dune's multichain `dex.trades` table.

Every SQL template ships with a `{{chain}}` parameter and is otherwise
chain-agnostic. The agent's three symbols map to the canonical
on-chain wraps of BTC / ETH / SOL on the chosen chain:

| Symbol     | Ethereum mainnet      | Base                  | Arbitrum One        |
|------------|-----------------------|-----------------------|---------------------|
| `BTC-PERP` | WBTC                  | cbBTC                 | WBTC                |
| `ETH-PERP` | WETH                  | WETH (`0x4200...0006`)| WETH                |
| `SOL-PERP` | Wormhole-wrapped SOL  | Wormhole-wrapped SOL  | Wormhole-wrapped SOL|

The token addresses are pre-loaded by `src/utils/config.py`. You can
override any of them via `DUNE_TOKEN_{BTC,ETH,SOL,USDC}_ADDRESS` in
`.env` to point at a different wrap, fork or sidechain.

To switch chains, set `DUNE_CHAIN=base` (or `arbitrum`) in `.env`
and re-save the queries with the matching chain default on Dune.
The same SQL covers every supported chain.

## How to wire it up

1. Open https://dune.com and log in with the same account whose
   `DUNE_API_KEY` you set in `.env`.
2. For each file in `dune/queries/*.sql`, do:
   1. **New query** → paste the SQL.
   2. **Add query parameters** the SQL references (see the header
      comment in each file). The common set is `chain`,
      `lookback_hours`, `btc_token_address`, `eth_token_address`,
      `sol_token_address`. Set defaults that match your `DUNE_CHAIN`.
   3. **Save** the query. Dune assigns a numeric query id (visible in
      the URL: `https://dune.com/queries/<id>`).
   4. (Optional but recommended.) Set a **schedule** so the query
      auto-refreshes at a reasonable cadence (e.g. every 30 minutes).
3. Copy each query id into `.env`:

   ```env
   # Level 1 - OHLCV (the trend / RSI / ATR / volatility filters live
   # on top of this query). Without it Level 1 honestly reports
   # `ohlcv_unavailable` and refuses to trade.
   DUNE_QUERY_OHLCV_ID=12340

   # Level 2 - on-chain intelligence
   DUNE_QUERY_FUNDING_RATES_ID=12345
   DUNE_QUERY_OPEN_INTEREST_ID=12346
   DUNE_QUERY_VOLUME_ID=12347
   DUNE_QUERY_VAULT_FLOWS_ID=12348
   DUNE_QUERY_WHALE_ACTIVITY_ID=12349
   DUNE_QUERY_LONG_SHORT_RATIO_ID=12350
   DUNE_QUERY_CUM_FUNDING_ID=12351
   DUNE_QUERY_MARKET_SENTIMENT_ID=12352
   ```

4. Restart the agent. Each panel cycle prints a **provenance map**
   (`dune:<id>` per metric). Metrics whose id you haven't filled in
   yet show as `n/a` and the rationale carries a clear note.

## A note on spot-derived perp metrics

Level 2 still measures funding / OI / L-S / cumulative funding even
though `dex.trades` is a spot tape. We compute each one as a clearly
labelled **proxy** that captures the same structural signal:

| Metric              | Proxy                                                                                                |
|---------------------|------------------------------------------------------------------------------------------------------|
| Funding rate        | Buy-vs-sell USD imbalance over 8h, scaled to a per-8h funding-rate equivalent (0.05% / 1.0 imbalance) |
| Open interest       | Rolling-USD volume + 1h / 4h / 24h deltas                                                            |
| Long/short ratio    | `sum(buy_usd) / sum(sell_usd)`, with unique-wallet counts for the account-ratio columns              |
| Cumulative funding  | Net signed aggressor flow scaled across the lookback window                                          |
| Whale activity      | Spot DEX trades above `whale_min_usd`, signed accumulating / distributing                            |
| Vault flows         | ERC-20 `Transfer` events into / out of `DUNE_PERP_VAULT_ADDRESS`                                     |
| Market sentiment    | `0.5 + 0.25 * mean(imbalance) + 0.5 * clip(mean(24h price change), ±0.25)`                            |

The SQL prefix-comments document each derivation in detail. When a
real perp-DEX schema lands on Dune (Arc, Synthetix V3 perps, etc.),
swap the `dex.trades` source for the perp's `fills` / `funding_events`
table and the rest of the pipeline stays unchanged.

## Query contracts

Each query is expected to return one row per perp symbol (apart from
`ohlcv` which returns one row per bucket, and `vault_flows` /
`market_sentiment`, which return a single row). The column names
below are the canonical schema CapitalArc parses; they match the SQL
templates 1:1 - **do not rename columns**.

### `ohlcv.sql` (Level 1)

Multi-row result: one row per `(symbol, interval, bucket_time)`.

| Column        | Type                                  | Notes                                |
|---------------|---------------------------------------|--------------------------------------|
| `symbol`      | text                                  | `BTC-PERP` / `ETH-PERP` / `SOL-PERP` |
| `interval`    | text                                  | `15m` / `1h`                         |
| `bucket_time` | timestamp / int (epoch seconds or ms) | UTC bucket left-edge                 |
| `open`        | double                                | first trade price in the bucket      |
| `high`        | double                                | max trade price                      |
| `low`         | double                                | min trade price                      |
| `close`       | double                                | last trade price                     |
| `volume`      | double                                | sum of token sizes                   |
| `fills`       | bigint                                | number of trades in the bucket       |

### `funding_rates.sql`

| Column                  | Type    | Notes                                                          |
|-------------------------|---------|----------------------------------------------------------------|
| `symbol`                | text    | `BTC-PERP` / `ETH-PERP` / `SOL-PERP`                          |
| `current_rate`          | double  | spot-imbalance-derived per-8h funding equivalent              |
| `rate_8h_change`        | double  | current minus previous 8h imbalance rate                       |
| `rate_24h_change`       | double  | current minus 16-24h imbalance rate                            |
| `weighted_average_24h`  | double  | mean imbalance over the window, scaled                         |
| `annualised_pct`        | double  | `current_rate * 3 * 365 * 100`                                  |

### `open_interest.sql`

| Column              | Type   | Notes                                |
|---------------------|--------|--------------------------------------|
| `symbol`            | text   |                                      |
| `current_contracts` | double | sum of base-token volume in last 1h  |
| `current_value_usd` | double | sum of USD volume in last 1h         |
| `delta_1h_pct`      | double | vs prior 1h                          |
| `delta_4h_pct`      | double | vs prior 3h hourly average           |
| `delta_24h_pct`     | double | vs prior 23h hourly average          |

### `volume.sql`

| Column                 | Type   | Notes                                                |
|------------------------|--------|------------------------------------------------------|
| `symbol`               | text   |                                                      |
| `last_price`           | double | most recent trade price (USD)                        |
| `price_change_pct_24h` | double | % change over the lookback window                    |
| `volume_24h_usd`       | double | sum of `amount_usd` over the window                  |
| `volume_1h_usd`        | double | sum of `amount_usd` in the latest hour               |

### `vault_flows.sql`

Single-row result.

| Column              | Type    | Notes                                                            |
|---------------------|---------|------------------------------------------------------------------|
| `tvl_usdc`          | double  | current USDC balance of `DUNE_PERP_VAULT_ADDRESS`                |
| `deposits_usdc`     | double  | sum of deposit notional in `lookback_hours`                      |
| `withdrawals_usdc`  | double  | sum of withdrawal notional in `lookback_hours`                   |
| `deposit_events`    | bigint  | number of deposit transfers                                       |
| `withdrawal_events` | bigint  | number of withdrawal transfers                                    |
| `window_hours`      | double  | should equal `lookback_hours`                                     |

### `whale_activity.sql`

| Column                | Type   | Notes                                              |
|-----------------------|--------|----------------------------------------------------|
| `symbol`              | text   |                                                    |
| `flagged`             | bool   | `true` when whale move detected                    |
| `direction`           | text   | `accumulating` / `distributing` / `neutral`        |
| `notional_usd_change` | double | signed sum of whale-sized trades                   |
| `rationale`           | text   | short human-readable note                          |

### `long_short_ratio.sql`

| Column              | Type   | Notes                                                |
|---------------------|--------|------------------------------------------------------|
| `symbol`            | text   |                                                      |
| `long_short_ratio`  | double | `sum(buy_usd) / sum(sell_usd)`                       |
| `long_account_pct`  | double | fraction of unique wallets that net-bought           |
| `short_account_pct` | double | fraction of unique wallets that net-sold             |

### `cum_funding.sql`

| Column           | Type   | Notes                                                 |
|------------------|--------|-------------------------------------------------------|
| `symbol`         | text   |                                                       |
| `longs_paid_usd` | double | scaled positive net flow over the window              |
| `shorts_paid_usd`| double | scaled negative net flow over the window              |
| `net_flow_usd`   | double | `longs_paid_usd - shorts_paid_usd` (signed)           |
| `window_hours`   | double | should equal `lookback_hours`                         |

### `market_sentiment.sql`

Single-row result.

| Column      | Type             | Notes                                                 |
|-------------|------------------|-------------------------------------------------------|
| `heat`      | double in [0, 1] | risk-on heat: 1.0 = full risk-on, 0.0 = full risk-off |
| `regime`    | text             | optional regime label                                 |
| `rationale` | text             | optional human-readable note                          |

## Why "Dune MCP only"?

CapitalArc is built for the Arc Perp DEX and the Circle stack. Adding
any off-chain feed (Binance / OKX / centralized OHLCV provider /
direct RPC scrapers) would couple the agent to infrastructure we do
not control and break the on-chain promise. Dune MCP gives us all
candles + flow metrics from the same indexed copy of the chain, with
shared caching, scheduling, and dashboards.

Concretely:

- **Level 1** (technical indicators) reads OHLCV through `ohlcv.sql`.
- **Level 2** (on-chain intelligence) reads every metric through its
  own saved query, on the chain configured by `DUNE_CHAIN`.
- **Arc RPC** is still used, but only for account state (wallet
  balance, agent margin in the vault, vault TVL) - it never feeds
  trading signals.

Until queries are saved, the agent honestly reports `n/a` for every
metric (and Level 1 blocks on `ohlcv_unavailable`). This is
intentional - the agent must never invent data.
