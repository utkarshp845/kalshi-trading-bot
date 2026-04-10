# Kalshi BTC Bot — Strategy Reference

## Overview

The bot looks for mispriced binary options on Kalshi's BTC daily price-level markets (series `KXBTC`). Each market pays $1 if BTC closes above a strike price at 4 PM ET, and $0 otherwise. The bot models the fair probability of that event using a log-normal price model, then buys YES or NO contracts when the market ask is far enough below the fair value to cover fees and leave a meaningful net edge.

---

## 1. Probability Model

### Formula

```
P(BTC_close > K) = Φ(d)

where:
  d = ln(S / K) / (σ × √T)

  S = current BTC spot price (USD)
  K = strike price (USD)
  T = time to expiry in years  (hours_remaining / 8760)
  σ = annualized realized volatility
  Φ = standard normal CDF
```

This is the log-normal binary option formula — the same form as the digital cash-or-nothing option in Black-Scholes, but with no drift term.

### Why no drift?

For very short horizons (intraday to 1 day), the drift contribution `μT` is negligible compared to the volatility term `σ√T`. At a 4-hour horizon with σ=0.65:

```
drift contribution ≈ μ × (4/8760) ≈ 0 to 0.001
vol contribution  ≈ 0.65 × √(4/8760) ≈ 0.044
```

The drift is ~50–100× smaller than volatility at these timescales, so omitting it is a sound approximation. For multi-day horizons it would need to be added.

### Volatility Inputs

Two lookback windows are used:

| Window | Config var | Default | Purpose |
|--------|-----------|---------|---------|
| Short | `VOL_SHORT_DAYS` | 7 days | Probability calculation — responsive to current regime |
| Long | `VOL_LONG_DAYS` | 30 days | Logged as regime reference, not used in signals |

**Why two?** A 30-day lookback lags sudden regime changes by weeks. If BTC volatility doubles over 3 days (e.g. a flash crash), the 7-day vol will reflect it; the 30-day vol will still be anchored to the calm period. The short vol makes the probability estimate more honest about the current environment.

The ratio `σ_short / σ_long` is logged each cycle. When this ratio is significantly above 1.0, the market is in a high-vol regime and edges may narrow or widen compared to normal.

Volatility is computed as annualized realized vol from daily closes:

```
log_returns = [ln(close_i / close_{i-1}) for i in 1..n]
daily_std   = sample_stddev(log_returns)       # n-1 denominator
annual_vol  = daily_std × √365
```

---

## 2. Edge and Signal Generation

### Edge Definition

For each open KXBTC market the bot computes:

```
gross_edge_yes = theo_prob - yes_ask
gross_edge_no  = (1 - theo_prob) - no_ask

net_edge_yes   = gross_edge_yes - fee_per_contract
net_edge_no    = gross_edge_no  - fee_per_contract
```

The `net_edge` is what you actually expect to earn per contract after paying the taker fee.

### Taker Fee

Kalshi charges a fee on each taker order. The default config is `KALSHI_TAKER_FEE = 0.07` ($0.07 per contract). This is deducted from the gross edge before any threshold comparison — so a signal with `gross_edge = 0.14` and `fee = 0.07` has `net_edge = 0.07`.

**Check your actual fee tier** in the Kalshi dashboard and update `KALSHI_TAKER_FEE` in `.env` accordingly. Higher-volume accounts pay lower fees.

### Signal Threshold

A trade is only triggered when:

```
net_edge > MIN_EDGE   (default: 0.08)
```

This means the expected profit per contract (after fees) must exceed $0.08. At a $0.15 ask price, you need to be right at least `(0.15 + 0.07 + 0.08) / 1.00 = 30%` of the time on a market the model says is 38%+ likely. The threshold filters out noise and thin edges.

### Market Filters

Before computing an edge, each market is filtered:

| Filter | Default | Reason |
|--------|---------|--------|
| Minimum time to expiry | 0.5 hours | Near-expiry markets have wide spreads and low liquidity |
| Must be able to parse strike from ticker | — | Skips unknown ticker formats |
| Already held | — | Avoids doubling into existing positions |

---

## 3. Position Sizing — Fractional Kelly

### Formula

```
kelly_f   = net_edge / (1 - ask_price)
spend     = kelly_f × KELLY_FRACTION × remaining_budget
contracts = floor(spend / ask_price)
```

### Derivation

For a binary contract that pays $1 with probability `p` and $0 otherwise, the Kelly criterion gives the optimal fraction `f` of bankroll to bet:

```
f* = (p × (1/ask - 1) - (1-p)) / (1/ask - 1)
   = (p - ask) / (1 - ask)
   = edge / (1 - ask)
```

This maximises the long-run growth rate of the bankroll.

### Fractional Kelly

Full Kelly is mathematically optimal but has extreme drawdowns in practice. The bot uses `KELLY_FRACTION = 0.25` (quarter Kelly), which reduces variance significantly at the cost of ~25% lower expected growth rate. This is a conservative but standard choice.

### Risk Gates (applied after sizing)

| Gate | Default | Effect |
|------|---------|--------|
| `MAX_DAILY_SPEND` | $100 | Hard cap on total daily spend |
| `MAX_CONTRACTS_PER_MARKET` | 10 | Per-market contract cap |
| `MAX_POSITIONS` | 5 | Max open positions at one time |

Sizing is clipped to `min(kelly_spend, remaining_budget)` and then to `MAX_CONTRACTS_PER_MARKET`. If the resulting contract count is 0, the trade is skipped.

---

## 4. Execution

- Orders are placed as **limit buys at the ask price**. This means the order will fill only if someone is willing to sell at that price (the current ask). In practice on liquid markets this fills quickly; on thin markets it may sit or partially fill.
- A **0.5s delay** is inserted between consecutive orders within the same cycle to avoid rate-limiting (429 errors).
- A **retry with exponential backoff** (up to 4 attempts: 1s, 2s, 4s, 8s) handles transient 429s at the API level.
- The bot polls every `POLL_INTERVAL_SECONDS` (default 300s = 5 minutes).

---

## 5. Fill Quality Tracking

Each cycle, the bot re-fetches the status of any order placed within the last 48 hours that hasn't been fully filled. For filled orders it computes and stores:

| Metric | Formula | Meaning |
|--------|---------|---------|
| `fill_price_dollars` | `taker_fill_cost / fill_count` | Actual average price paid per contract |
| `slippage` | `fill_price - entry_ask` | Positive = paid more than the ask (unusual for limits) |
| `realized_edge` | `theo_prob - fill_price - fee` | Net edge you actually captured |

These are stored in the `orders` SQLite table and visible in `logs/trades.csv`. After a week of trading you can query:

```sql
SELECT
    AVG(gross_edge)    AS avg_gross_edge,
    AVG(edge)          AS avg_net_edge,
    AVG(realized_edge) AS avg_realized_edge,
    AVG(slippage)      AS avg_slippage,
    COUNT(*)           AS trades
FROM orders
WHERE fill_count > 0;
```

If `avg_realized_edge` is consistently below `avg_net_edge`, your probability model is mis-calibrated (you're overstating edge). If they're close, the model is working.

---

## 6. Data Flow

```
Kraken /Ticker  ──→  BTC spot price (S)

Kraken /OHLC    ──→  7-day closes  ──→  σ_short (for probability)
                ──→  30-day closes ──→  σ_long  (regime reference)

Kalshi /markets ──→  Market list: K, yes_ask, no_ask, close_time

calc_prob(S, K, T, σ_short)  ──→  theo_prob

gross_edge = theo_prob - ask
net_edge   = gross_edge - KALSHI_TAKER_FEE

if net_edge > MIN_EDGE:
    contracts = floor(kelly_f × 0.25 × remaining_budget / ask)
    place_order(contracts @ ask)

→ fill quality checked next cycle
→ results stored in data/bot.db + logs/trades.csv
```

---

## 7. Known Limitations

### Realized vol ≠ Implied vol
The model uses historical realized volatility. Kalshi market makers may price in forward-looking implied vol (e.g. ahead of macro events). On days when implied vol > realized vol, the model will see false edges — the market looks mispriced but the market maker has better information.

**Mitigation:** The `σ_short / σ_long` ratio in the logs gives a rough view of vol regime. Edges generated during high-ratio periods (short vol >> long vol) should be treated with more skepticism.

### No position exit logic
The bot only enters. If the edge inverts after entry, there is no automatic exit — positions run to settlement at 4 PM ET. This is fine for a hold-to-settlement strategy but means a bad entry is fully exposed.

### Partial fills
Limit orders may partially fill. The bot records `fill_count` and `taker_fill_cost` from the API, and the fill quality check tracks actual vs. expected. However, the risk module sizes based on the intended count, not the actual fill — a persistent partial-fill environment will leave you under-deployed.

### Independent positions assumption
The Kelly formula assumes each position is independent. In practice, all KXBTC positions are correlated — they're all bets on the same underlying (BTC price at 4 PM). Multiple simultaneous positions at different strikes are not independent bets.

---

## 8. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_TAKER_FEE` | 0.07 | Taker fee in dollars per contract |
| `MIN_EDGE` | 0.08 | Minimum net edge to place a trade |
| `MIN_T_HOURS` | 0.5 | Skip markets expiring sooner than this |
| `VOL_SHORT_DAYS` | 7 | Fast vol lookback for signal probability |
| `VOL_LONG_DAYS` | 30 | Slow vol lookback for regime reference |
| `MAX_DAILY_SPEND` | 100 | Max dollars to spend per calendar day |
| `MAX_CONTRACTS_PER_MARKET` | 10 | Max contracts per single market |
| `MAX_POSITIONS` | 5 | Max concurrent open positions |
| `KELLY_FRACTION` | 0.25 | Fraction of full Kelly to bet |
| `POLL_INTERVAL_SECONDS` | 300 | Seconds between cycles |
| `DRY_RUN` | false | Log signals only, no real orders |
| `FORCE_TRADING_HOURS` | false | Bypass 9 AM–3:30 PM ET trading window guard |
