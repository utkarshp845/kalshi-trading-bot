# Kalshi BTC Bot — Strategy Reference

## Overview

The bot looks for mispriced binary options on Kalshi's `KXBTC` series: contracts that pay $1 if BTC closes above a given strike price at 4 PM ET, and $0 otherwise. It prices each contract with a log-normal model, measures the gap between realized and implied volatility directly from live Kalshi prices, and trades when the net edge is large enough to survive both fees and model uncertainty.

---

## 1. Probability Model

### Formula

```
P(BTC_close > K) = Φ(d)

where:
  d = ln(S / K) / (σ_adjusted × √T)

  S            = current BTC spot price (USD, from Kraken)
  K            = strike price (USD, parsed from ticker)
  T            = time to expiry in years  (hours_remaining / 8760)
  σ_adjusted   = realized vol × adaptive safety margin
  Φ            = standard normal CDF
```

This is the log-normal digital cash-or-nothing formula — Black-Scholes for a binary payoff, without a drift term.

### Why no drift?

For intraday horizons the drift is negligible. At 4 hours with σ = 0.65:

```
drift ≈ μ × (4/8760) ≈ 0–0.001
vol   ≈ 0.65 × √(4/8760) ≈ 0.044
```

Drift is 50–100× smaller than volatility at these timescales, so omitting it is a sound approximation.

### Volatility Inputs

Two lookback windows are computed from Kraken daily OHLC data:

| Window | Config var | Default | Purpose |
|--------|-----------|---------|---------|
| Short | `VOL_SHORT_DAYS` | 7 days | Probability calculation — responsive to current regime |
| Long | `VOL_LONG_DAYS` | 30 days | Regime reference and vol ratio denominator |

The ratio `σ_short / σ_long` is computed each cycle. When this exceeds `MAX_VOL_RATIO` (default 1.8), the bot **skips the entire cycle** — the model is unreliable during extreme regime transitions.

Volatility is annualized realized vol from log returns:

```
log_returns = [ln(close_i / close_{i-1}) for i in 1..n]
daily_std   = sample_stddev(log_returns)      # Bessel-corrected (n-1 denom)
annual_vol  = daily_std × √365
```

---

## 2. Implied Volatility Calibration

### Motivation

The model uses historical realized vol. Market makers price in *forward-looking* implied vol — especially ahead of macro events. On those days, Kalshi markets appear mispriced but actually reflect better information. The static `VOL_SAFETY_MARGIN` (25% inflation) partially corrects this, but a data-driven measurement is more honest.

### IV Back-out

Each Kalshi market's mid-price encodes market consensus probability. Inverting the log-normal formula recovers the implied volatility:

```
P = Φ( ln(S/K) / (σ × √T) )

⟹  σ_impl = ln(S/K) / (Φ⁻¹(P_mid) × √T)
```

Reliability gates applied before using a result:

| Gate | Reason |
|------|--------|
| `0.03 ≤ mid ≤ 0.97` | Deep ITM/OTM markets are ill-conditioned — many (σ, K) pairs yield the same extreme probability |
| `\|d_implied\| ≥ 0.10` | Too close to ATM — result is highly sensitive to small price moves |
| `sign(d) = sign(ln(S/K))` | Sign disagreement means the market price is internally inconsistent |
| Round-trip check `\|P(σ_impl) - mid\| ≤ 0.02` | Sanity check that the back-out is self-consistent |
| `0.10 ≤ σ_impl ≤ 8.00` | Hard bounds on plausible annual vol |
| Percentage spread ≤ 30% | Wide-spread markets have unreliable midpoints |

### Cycle-level IV/RV Ratio

After back-out across all valid markets, the cycle-level implied vol is computed as a **spread-weighted median** of per-market implied vols. Markets with tighter spreads get higher weight (`1 / spread`), since their midpoints are more informative.

```
iv_rv_ratio = weighted_median(σ_impl values) / σ_short
```

Requires ≥ 3 valid markets; otherwise falls back to the static safety margin.

### Adaptive Safety Margin

The bot maintains a rolling median of the last 10–20 `iv_rv_ratio` observations. This becomes the next cycle's volatility inflation factor:

```
σ_adjusted = σ_short × clamp(rolling_median_iv_rv_ratio,
                              IV_SAFETY_MARGIN_MIN,
                              IV_SAFETY_MARGIN_MAX)
```

Default clamp: `[1.05, 3.0]`. During normal market conditions the ratio hovers around 1.1–1.3. During event risk (FOMC, CPI) it can reach 2+. The adaptive margin responds automatically rather than requiring manual adjustment.

The `iv_rv_ratio` and `adaptive_safety_margin` used each cycle are stored in the `runs` SQLite table for post-hoc analysis.

---

## 3. Edge and Signal Generation

### Edge Definition

For each open `KXBTC` market:

```
gross_edge_yes = theo_prob - yes_ask
gross_edge_no  = (1 - theo_prob) - no_ask

net_edge_yes   = gross_edge_yes - KALSHI_TAKER_FEE
net_edge_no    = gross_edge_no  - KALSHI_TAKER_FEE
```

`net_edge` is the expected profit per contract after paying the taker fee.

### Taker Fee

Default `KALSHI_TAKER_FEE = 0.07` ($0.07 per contract). This is deducted before any threshold comparison. A trade with gross edge 0.14 and fee 0.07 has net edge 0.07 — below the default threshold. **Check your actual fee tier in the Kalshi dashboard and update accordingly.** Higher-volume accounts pay lower fees; under-charging the fee inflates apparent edge.

### Signal Threshold

```
net_edge > MIN_EDGE   (default: 0.15)
```

Only trades with at least $0.15 expected profit per contract (after fees) are taken. This is intentionally high to account for model uncertainty.

### Market Filters

All filters are applied before edge computation:

| Filter | Default | Reason |
|--------|---------|--------|
| Time to expiry | ≥ 1.0 hours | Near-expiry markets have wide spreads and low liquidity |
| Absolute bid-ask spread | ≤ 0.25 | Wide spreads create phantom edges |
| Percentage bid-ask spread | ≤ 30% of mid | Eliminates illiquid markets where the mid is unreliable |
| `last_price` divergence | ≤ $0.15 from mid | Stale or rapidly-moving markets where the mid is stale |
| Vol regime | σ_short/σ_long ≤ 1.8 | Skip entire cycle during unstable transitions |
| Ticker parseable | — | Skip unknown ticker formats |
| Already held | — | No doubling into existing positions |

Both YES and NO sides are evaluated independently. The side with the higher net edge is taken, provided it passes spread filters.

---

## 4. Position Sizing — Fractional Kelly

### Formula

```
kelly_f   = net_edge / (1 - ask_price)
spend     = kelly_f × KELLY_FRACTION × effective_budget
contracts = floor(spend / ask_price)
```

### Derivation

For a binary contract paying $1 with probability `p`, the Kelly criterion maximises long-run bankroll growth at:

```
f* = (p - ask) / (1 - ask)  =  edge / (1 - ask)
```

### Fractional Kelly

Full Kelly has extreme drawdowns in practice. `KELLY_FRACTION = 0.10` (tenth Kelly) dramatically reduces variance while preserving most of the growth benefit. Given model uncertainty in vol estimates, tenth Kelly is appropriate.

### Balance-Aware Budget

```
effective_budget = min(remaining_daily_cap, BANKROLL_FRACTION × actual_balance)
```

Prevents a $5 daily cap from being meaningless on a $3 account. Never risks more than 25% of actual balance in a single day.

### Correlation Discount

All KXBTC positions bet on the same underlying. Standard Kelly assumes independence. Each additional open position reduces sizing by 30%:

```
adjusted_spend = kelly_spend × 0.7^open_positions
```

### Drawdown Guard

If balance falls below `(1 - MAX_DRAWDOWN_PCT) × session_start_balance` (default: 20% drawdown), **all trading halts for the day.** This prevents loss-chasing.

### Risk Gates

| Gate | Default | Effect |
|------|---------|--------|
| `MAX_DAILY_SPEND` | $5 | Hard cap on total daily spend |
| `MAX_CONTRACTS_PER_MARKET` | 3 | Per-market contract cap |
| `MAX_POSITIONS` | 2 | Max concurrent open positions |
| `MAX_DRAWDOWN_PCT` | 20% | Halt if account drops this much from session start |
| `BANKROLL_FRACTION` | 25% | Never risk more than this of actual balance per day |

---

## 5. Execution

### Price Improvement

Before paying the ask, the bot attempts a **mid-price fill** (at `(bid + ask) / 2`):

1. Place limit buy at mid-price
2. Wait `PRICE_IMPROVEMENT_TIMEOUT_SEC` seconds (default: 45s)
3. Check how much filled
4. Cancel remainder; re-fill at ask for any unfilled contracts

Both the mid-fill and the ask-fill orders are tracked separately. Their costs are summed for balance accounting and risk tracking — **partial mid fills are never discarded**. This ensures blended fill costs are reflected accurately in the daily risk cap.

A 0.5s delay between consecutive orders within the same cycle prevents burst rate-limiting.

### Order Type

All buys are **limit orders at the ask price** (or mid-price on the improvement attempt). This means fills are guaranteed at that price or better, never worse.

---

## 6. Position Management

### Intra-day Exit

Each cycle, the bot re-fetches all open positions. If the current theoretical value of a position drops to ≤ 40% of its entry price:

```
exit_trigger = theo_value ≤ EXIT_LOSS_TRIGGER × entry_price
              (default EXIT_LOSS_TRIGGER = 0.40)
```

A limit sell is placed at the best bid. This recovers some value rather than riding to a $0 settlement. Enable/disable with `ENABLE_POSITION_EXIT`.

### Cycle Schedule

The bot runs every `POLL_INTERVAL_SECONDS` (default: 120s) during trading hours (9 AM – 3:30 PM ET). Market hours guard can be bypassed with `FORCE_TRADING_HOURS = true` for testing.

---

## 7. Fill Quality and P&L Tracking

### Per-order Metrics

Each cycle, open orders from the last 48 hours are re-fetched. For filled orders:

| Metric | Formula | Meaning |
|--------|---------|---------|
| `fill_price_dollars` | `taker_fill_cost / fill_count` | Actual average price paid |
| `slippage` | `fill_price - entry_ask` | Positive = paid more than expected |
| `realized_edge` | `theo_prob - fill_price - fee` | Net edge actually captured |

### Settlement Tracking

When Kalshi settles a contract, the bot records:

- `settled_value = 1.0` → contract won (position paid out)
- `settled_value = 0.0` → contract lost

This enables probability calibration: if `AVG(settled_value - theo_prob)` diverges significantly from zero, the model is systematically biased.

### Self-Calibration

After ≥ 30 settled trades, the bot computes:

```
prob_bias = AVG(settled_value - theo_prob)
```

- Positive bias → model under-predicts winning probability → `VOL_SAFETY_MARGIN` is too high
- Negative bias → model over-predicts → margin is too low

This is currently informational (logged each cycle). Combined with the adaptive IV/RV margin, it forms a two-layer calibration system.

### Useful Queries

```sql
-- Average realized vs predicted edge
SELECT AVG(gross_edge), AVG(edge), AVG(realized_edge), AVG(slippage), COUNT(*)
FROM orders WHERE fill_count > 0;

-- Calibration bias (run after 30+ settled trades)
SELECT AVG(settled_value - theo_prob) AS bias, COUNT(*) AS n
FROM orders WHERE settled_value IS NOT NULL AND theo_prob IS NOT NULL;

-- Recent IV/RV regime
SELECT run_at, iv_rv_ratio, adaptive_safety_margin
FROM runs ORDER BY run_at DESC LIMIT 20;
```

---

## 8. Daily Reports

Each cycle, the bot writes `reports/YYYY-MM-DD.md` with a full day summary. Sections:

- **Summary** — realized P&L (settled trades), capital deployed, orders placed/filled/canceled, EOD balance and daily spend
- **Trades Opened** — time, ticker, side, qty, fill, cost, edge, status
- **Settlements** — W/L table, win rate, per-trade P&L
- **Fill Quality** — avg slippage, predicted vs realized edge, edge leak (difference between the two)
- **Model Calibration** — today's settled-trade bias with directional nudge if > ±5%
- **Market Context** — BTC range, vol regime ratio, IV/RV ratio, adaptive safety margin, signals-to-orders conversion rate
- **Notable Trades** — best and worst of the day

Generate manually for any date:

```bash
python -m bot.report                    # today (UTC)
python -m bot.report --date 2026-04-15  # specific date
```

---

## 9. Data Flow

```
Kraken /Ticker  ──→  BTC spot price (S)

Kraken /OHLC    ──→  7-day closes  ──→  σ_short
                ──→  30-day closes ──→  σ_long

── Vol regime check: σ_short/σ_long > MAX_VOL_RATIO? → SKIP CYCLE

Kalshi /markets ──→  Market mids
                ──→  backout_sigma() per market
                ──→  weighted median → iv_rv_ratio
                ──→  rolling median over last N cycles → adaptive_margin

σ_adjusted = σ_short × adaptive_margin
             (falls back to σ_short × VOL_SAFETY_MARGIN until N ≥ IV_CALIBRATION_MIN_OBS)

── Drawdown check: balance < (1 − MAX_DRAWDOWN_PCT) × session_start? → HALT

── Position exit check: theo_value ≤ 0.40 × entry_price? → SELL

Kalshi /markets ──→  Filter: spread ≤ limits, T ≥ MIN_T_HOURS, last_price OK
                ──→  calc_prob(S, K, T, σ_adjusted)  →  theo_prob
                ──→  gross_edge = theo_prob - ask;  net_edge = gross_edge - fee

if net_edge > MIN_EDGE and risk gates pass:
    budget = min(remaining_daily, BANKROLL_FRACTION × balance)
    kelly_spend = (edge / (1-ask)) × KELLY_FRACTION × budget × 0.7^open_positions
    contracts = floor(kelly_spend / ask)

    Phase 1: place limit @ mid-price, wait 45s
    Phase 2: cancel remainder, fill at ask
    → both fills logged separately, costs summed for risk accounting

→ fill quality re-checked next cycle
→ data/bot.db (SQLite) + logs/trades.csv
→ reports/YYYY-MM-DD.md (refreshed each cycle)
```

---

## 10. Known Limitations

### Realized vol lags implied vol

The model is backward-looking by design. On event-risk days (FOMC, CPI, ETF news), market makers price in a forward-looking vol premium that realized vol underestimates. The adaptive IV/RV margin and minimum edge threshold are the primary mitigations; neither is perfect.

### Partial fills leave you under-deployed

Limit orders may partially fill. The risk module sizes based on the intended contract count. In illiquid markets with persistent partial fills, actual exposure is lower than the model intended. The fill-quality tracker exposes this, but there is no automatic re-sizing.

### Correlation is a heuristic

The `0.7^open_positions` discount is an approximation. True BTC contract correlations depend on strike distance and time to expiry — a near-ATM contract and a deep OTM contract are not equally correlated. The heuristic prevents the worst over-sizing but is not theoretically grounded.

### Settlement timing ambiguity

Kalshi marks settlement asynchronously. The `fill_count > 0` heuristic for WIN detection assumes Kalshi zeros fill_count on losing contracts at settlement, which matches observed behavior but is inferred from the API rather than officially documented.

---

## 11. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_TAKER_FEE` | 0.07 | Taker fee in dollars per contract |
| `MIN_EDGE` | 0.15 | Minimum net edge (after fees) to place a trade |
| `MIN_T_HOURS` | 1.0 | Skip markets expiring within this many hours |
| `VOL_SHORT_DAYS` | 7 | Fast vol lookback for probability calculation |
| `VOL_LONG_DAYS` | 30 | Slow vol lookback for regime reference |
| `VOL_SAFETY_MARGIN` | 1.25 | Static vol multiplier (used until IV calibration has enough data) |
| `MAX_VOL_RATIO` | 1.8 | Skip cycle when σ_short/σ_long exceeds this |
| `MAX_BID_ASK_SPREAD` | 0.25 | Skip markets with absolute spread wider than this |
| `MAX_BID_ASK_PCT_SPREAD` | 0.30 | Skip markets with spread > 30% of mid |
| `MAX_LAST_PRICE_DIVERGENCE` | 0.15 | Skip if last_price diverges > $0.15 from mid |
| `IV_CALIBRATION_MIN_OBS` | 10 | Cycles before switching from static to adaptive margin |
| `IV_SAFETY_MARGIN_MIN` | 1.05 | Floor for adaptive vol margin |
| `IV_SAFETY_MARGIN_MAX` | 3.0 | Ceiling for adaptive vol margin |
| `MAX_DAILY_SPEND` | 5 | Max dollars to spend per calendar day |
| `MAX_CONTRACTS_PER_MARKET` | 3 | Max contracts per single market |
| `MAX_POSITIONS` | 2 | Max concurrent open positions |
| `KELLY_FRACTION` | 0.10 | Fraction of full Kelly to bet |
| `MAX_DRAWDOWN_PCT` | 0.20 | Halt trading if account drops 20% from session start |
| `BANKROLL_FRACTION` | 0.25 | Never risk more than 25% of actual balance per day |
| `ENABLE_POSITION_EXIT` | true | Exit losing positions intra-day |
| `EXIT_LOSS_TRIGGER` | 0.40 | Exit when value drops to 40% of entry price |
| `ENABLE_PRICE_IMPROVEMENT` | true | Try mid-price fill before paying full ask |
| `PRICE_IMPROVEMENT_TIMEOUT_SEC` | 45 | Wait this long for mid-price fill |
| `POLL_INTERVAL_SECONDS` | 120 | Seconds between cycles |
| `DRY_RUN` | false | Log signals only, no real orders |
| `FORCE_TRADING_HOURS` | false | Bypass the 9 AM–3:30 PM ET trading window guard |
| `ALERT_WEBHOOK_URL` | _(empty)_ | Slack/Discord webhook; empty = log-only |
