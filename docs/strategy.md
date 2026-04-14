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
| Long | `VOL_LONG_DAYS` | 30 days | Regime reference and vol ratio denominator |

**Why two?** A 30-day lookback lags sudden regime changes by weeks. If BTC volatility doubles over 3 days (e.g. a flash crash), the 7-day vol will reflect it; the 30-day vol will still be anchored to the calm period. The short vol makes the probability estimate more honest about the current environment.

The ratio `σ_short / σ_long` is logged each cycle. When this ratio exceeds `MAX_VOL_RATIO` (default 1.8), the bot **skips the entire cycle** — the model is unreliable in extreme regime transitions.

### Volatility Safety Margin

The bot inflates the short-window realized vol by `VOL_SAFETY_MARGIN` (default 1.25 = +25%) before computing probabilities. This accounts for the systematic gap between backward-looking realized vol and forward-looking implied vol that market makers price in. Without this buffer, the bot overestimates edges and takes trades that are actually fairly priced.

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
net_edge > MIN_EDGE   (default: 0.15)
```

This means the expected profit per contract (after fees) must exceed $0.15. The threshold is intentionally high to account for model uncertainty — the vol estimate is imperfect, so only trades with a large margin of safety are taken.

### Market Filters

Before computing an edge, each market is filtered:

| Filter | Default | Reason |
|--------|---------|--------|
| Minimum time to expiry | 1.0 hours | Near-expiry markets have wide spreads and low liquidity |
| Bid-ask spread | ≤ 0.25 | Wide spreads signal illiquidity and create phantom edges |
| Vol regime ratio | ≤ 1.8 | Extreme short/long vol ratio means model is unreliable |
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

Full Kelly is mathematically optimal but has extreme drawdowns in practice. The bot uses `KELLY_FRACTION = 0.10` (tenth Kelly), which dramatically reduces variance. Given that the edge estimates carry significant model uncertainty (realized vs implied vol), aggressive Kelly sizing destroys small bankrolls.

### Balance-Aware Sizing

The effective budget for each trade is `min(remaining_daily_cap, BANKROLL_FRACTION * actual_balance)`. This ensures the bot never risks more than 25% of actual account balance in a single day, regardless of the daily spend cap. This prevents a $5 daily cap from being meaningless on a $3 account.

### Correlation Discount

All KXBTC positions bet on the same underlying (BTC price). Standard Kelly assumes independent bets. To compensate, each additional open position reduces sizing by 30%:

```
effective_spend = kelly_spend * 0.7^open_positions
```

### Drawdown Guard

If the account balance drops below `(1 - MAX_DRAWDOWN_PCT)` of the session start balance (default: 20% drawdown), **all trading halts for the day**. This prevents the bot from chasing losses.

### Risk Gates (applied after sizing)

| Gate | Default | Effect |
|------|---------|--------|
| `MAX_DAILY_SPEND` | $5 | Hard cap on total daily spend |
| `MAX_CONTRACTS_PER_MARKET` | 3 | Per-market contract cap |
| `MAX_POSITIONS` | 2 | Max open positions at one time |
| `MAX_DRAWDOWN_PCT` | 20% | Stop trading if account drops this much |
| `BANKROLL_FRACTION` | 25% | Never risk more than this % of balance per day |

Sizing is clipped to `min(kelly_spend, remaining_budget, bankroll_limit)` and then to `MAX_CONTRACTS_PER_MARKET`. If the resulting contract count is 0, the trade is skipped.

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

Kraken /OHLC    ──→  7-day closes  ──→  σ_short
                ──→  30-day closes ──→  σ_long

── Vol regime check: σ_short/σ_long > MAX_VOL_RATIO? → SKIP CYCLE

σ_adjusted = σ_short × VOL_SAFETY_MARGIN

── Drawdown check: balance < (1 - MAX_DRAWDOWN_PCT) × session_start? → HALT

Kalshi /markets ──→  Market list: K, yes_ask, no_ask, close_time
                ──→  Filter: spread ≤ MAX_BID_ASK_SPREAD, T ≥ MIN_T_HOURS

calc_prob(S, K, T, σ_adjusted)  ──→  theo_prob

gross_edge = theo_prob - ask
net_edge   = gross_edge - KALSHI_TAKER_FEE

if net_edge > MIN_EDGE:
    budget = min(remaining_daily, BANKROLL_FRACTION × balance)
    kelly_spend × 0.7^open_positions  (correlation discount)
    contracts = floor(spend / ask)
    place_order(contracts @ ask)

→ fill quality checked next cycle
→ results stored in data/bot.db + logs/trades.csv
```

---

## 7. Known Limitations

### Realized vol ≠ Implied vol
The model uses historical realized volatility. Kalshi market makers may price in forward-looking implied vol (e.g. ahead of macro events). On days when implied vol > realized vol, the model will see false edges — the market looks mispriced but the market maker has better information.

**Mitigations (implemented):**
- `VOL_SAFETY_MARGIN` (default 1.25) inflates realized vol by 25%, partially closing the gap with implied vol
- `MAX_VOL_RATIO` (default 1.8) skips trading entirely when the vol regime is unstable
- `MIN_EDGE` raised to 0.15 to require a larger margin of safety

### No position exit logic
The bot only enters. If the edge inverts after entry, there is no automatic exit — positions run to settlement at 4 PM ET. The `MAX_DRAWDOWN_PCT` guard stops new entries if losses accumulate, but existing positions are not exited early.

### Partial fills
Limit orders may partially fill. The bot records `fill_count` and `taker_fill_cost` from the API, and the fill quality check tracks actual vs. expected. However, the risk module sizes based on the intended count, not the actual fill — a persistent partial-fill environment will leave you under-deployed.

### Correlated positions (partially mitigated)
The Kelly formula assumes each position is independent. In practice, all KXBTC positions are correlated. The correlation discount (`0.7^open_positions`) reduces sizing for each additional position, but is a heuristic — the true correlation structure is more complex.

---

## 8. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_TAKER_FEE` | 0.07 | Taker fee in dollars per contract |
| `MIN_EDGE` | 0.15 | Minimum net edge to place a trade |
| `MIN_T_HOURS` | 1.0 | Skip markets expiring sooner than this |
| `VOL_SHORT_DAYS` | 7 | Fast vol lookback for signal probability |
| `VOL_LONG_DAYS` | 30 | Slow vol lookback for regime reference |
| `VOL_SAFETY_MARGIN` | 1.25 | Multiply realized vol by this before pricing (accounts for implied > realized) |
| `MAX_VOL_RATIO` | 1.8 | Skip trading when σ_short/σ_long exceeds this (unstable regime) |
| `MAX_BID_ASK_SPREAD` | 0.25 | Skip markets with bid-ask spread wider than this |
| `MAX_DAILY_SPEND` | 5 | Max dollars to spend per calendar day |
| `MAX_CONTRACTS_PER_MARKET` | 3 | Max contracts per single market |
| `MAX_POSITIONS` | 2 | Max concurrent open positions |
| `KELLY_FRACTION` | 0.10 | Fraction of full Kelly to bet |
| `MAX_DRAWDOWN_PCT` | 0.20 | Stop trading if account drops 20% from session start |
| `BANKROLL_FRACTION` | 0.25 | Never risk more than 25% of actual balance per day |
| `POLL_INTERVAL_SECONDS` | 120 | Seconds between cycles |
| `DRY_RUN` | false | Log signals only, no real orders |
| `FORCE_TRADING_HOURS` | false | Bypass 9 AM–3:30 PM ET trading window guard |
