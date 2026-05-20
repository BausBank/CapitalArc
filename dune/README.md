# Dune queries for CapitalArc Level 2

CapitalArc's Level 2 is sourced **exclusively** from Dune MCP. The agent
queries a set of saved Dune queries by id; this folder ships the SQL
templates you save into your own Dune workspace.

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
`vault_flows` and `market_sentiment`, which return a single row). The
column names below are the canonical schema CapitalArc parses; they
match the SQL templates 1:1 - **do not rename columns**.

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

## Why "no Binance"?

CapitalArc is built for the Arc Perp DEX and the Circle stack. Adding a
CEX feed (Binance / OKX / ...) would couple the agent to off-chain
infrastructure we do not control and break the Arc-native promise. Dune
MCP gives us the same metrics, indexed *from* the chain itself.

Until queries are saved, Level 2 honestly reports `n/a` for every
metric and the agent still functions (Level 1 + the on-chain panel
keep working). This is intentional - the agent must never invent data.
