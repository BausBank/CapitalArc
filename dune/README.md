# Dune queries for CapitalArc

CapitalArc sources **all market analysis** (Level 1 OHLCV + Level 2
on-chain intelligence) **exclusively** from Dune MCP. The agent
queries a set of saved Dune queries by id; this folder ships the SQL
templates you save into your own Dune workspace.

Arc RPC is **not** used for market data; it only serves account state
(agent wallet balance, vault TVL/margin reads in the Market Context
panel). Every candle, every funding rate, every OI delta - all of it
comes from Dune.

## How to wire it up

1. Open https://dune.com and log in with the same account whose
   `DUNE_API_KEY` you set in `.env`.
2. For each file in `dune/queries/*.sql`, do:
   1. **New query** → paste the SQL.
   2. **Add query parameters** the SQL references (see the header
      comment in each file; usually `chain`, `lookback_hours`,
      `symbols`).
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

## Query contracts

Each query is expected to return one row per perp symbol (apart from
`ohlcv` which returns one row per bucket, and `vault_flows` /
`market_sentiment`, which return a single row). The column names
below are the canonical schema CapitalArc parses; they match the SQL
templates 1:1 - **do not rename columns**.

### `ohlcv.sql` (Level 1)

Multi-row result: one row per `(symbol, interval, bucket_time)`.

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | `BTC-PERP` / `ETH-PERP` / `SOL-PERP` |
| `interval` | text | `15m` / `1h` (extend the template for more) |
| `bucket_time` | timestamp / int (epoch seconds or ms) | UTC bucket left-edge |
| `open` | float | first fill price in the bucket |
| `high` | float | max fill price |
| `low` | float | min fill price |
| `close` | float | last fill price |
| `volume` | float | sum of fill sizes |
| `fills` | int | number of fills in the bucket (optional) |

### `funding_rates.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | `BTC-PERP` / `ETH-PERP` / `SOL-PERP` |
| `current_rate` | float | most recent funding rate (per 8h, e.g. `0.0001`) |
| `rate_8h_change` | float | current minus previous funding rate |
| `rate_24h_change` | float | current minus funding rate 24h ago |
| `weighted_average_24h` | float | volume-weighted average over the last 24h |
| `annualised_pct` | float | `current_rate * 3 * 365 * 100` |

### `open_interest.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | |
| `current_contracts` | float | |
| `current_value_usd` | float | OI x mark price |
| `delta_1h_pct` | float | |
| `delta_4h_pct` | float | |
| `delta_24h_pct` | float | |

### `volume.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | |
| `last_price` | float | |
| `price_change_pct_24h` | float | |
| `volume_24h_usd` | float | sum of notional over 24h |
| `volume_1h_usd` | float | sum of notional over the latest hour |

### `vault_flows.sql`

Single-row result.

| Column | Type | Notes |
|--------|------|-------|
| `tvl_usdc` | float | current USDC balance held by `USDCCollateralVault` |
| `deposits_usdc` | float | sum of deposit notional in `lookback_hours` |
| `withdrawals_usdc` | float | sum of withdrawal notional in `lookback_hours` |
| `deposit_events` | int | |
| `withdrawal_events` | int | |
| `window_hours` | float | should equal `lookback_hours` |

### `whale_activity.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | |
| `flagged` | bool | `true` when whale move detected |
| `direction` | text | `accumulating` / `distributing` / `neutral` |
| `notional_usd_change` | float | size of the move |
| `rationale` | text | short human-readable note |

### `long_short_ratio.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | |
| `long_short_ratio` | float | longs / shorts notional ratio |
| `long_account_pct` | float | fraction of accounts net-long |
| `short_account_pct` | float | fraction of accounts net-short |

### `cum_funding.sql`

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | text | |
| `longs_paid_usd` | float | total USD paid by longs over the window |
| `shorts_paid_usd` | float | total USD paid by shorts over the window |
| `net_flow_usd` | float | `longs_paid_usd - shorts_paid_usd` |
| `window_hours` | float | should equal `lookback_hours` |

### `market_sentiment.sql`

Single-row result.

| Column | Type | Notes |
|--------|------|-------|
| `heat` | float in [0, 1] | risk-on heat: 1.0 = full risk-on, 0.0 = full risk-off |
| `regime` | text | optional regime label |
| `rationale` | text | optional human-readable note |

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
  own saved query.
- **Arc RPC** is still used, but only for account state (wallet
  balance, agent margin in the vault, vault TVL) - it never feeds
  trading signals.

Until queries are saved, the agent honestly reports `n/a` for every
metric (and Level 1 blocks on `ohlcv_unavailable`). This is
intentional - the agent must never invent data.
