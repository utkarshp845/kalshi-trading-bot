# Kalshi BTC Arbitrage Bot

A mispricing arbitrage bot for [Kalshi](https://kalshi.com) BTC daily price-level markets. It prices binary contracts with a log-normal model, measures the gap between realized and implied volatility directly from market prices, and trades when the edge is large enough to survive model uncertainty.

## How it works

Kalshi's `KXBTC` series are binary contracts that pay $1 if BTC closes above a given strike price at 4 PM ET. Each cycle the bot:

1. **Fetches** BTC spot price + 7-day and 30-day realized volatility from Kraken
2. **Backs out implied vol** from near-ATM Kalshi market prices — replacing the static safety margin with a market-measured IV/RV ratio that adapts to event risk automatically
3. **Prices** each contract with the IV-adjusted volatility:
   ```
   P(BTC > K) = Φ( ln(S/K) / (σ_adjusted × √T) )
   ```
4. **Filters** markets: time to expiry ≥ 1 hour, bid-ask spread ≤ 25% of mid-price, `last_price` within $0.15 of mid, vol regime stable (σ_7d/σ_30d ≤ 1.8)
5. **Trades** the best opportunity when `net_edge > MIN_EDGE`, sized by fractional Kelly with balance-awareness and a correlation discount
6. **Manages positions**: checks open positions each cycle and exits via limit sell if theoretical value drops to ≤40% of entry price
7. **Tries to improve fills**: attempts mid-price first, falls back to ask after 45 seconds
8. **Self-calibrates**: after 30+ settled trades, adjusts `VOL_SAFETY_MARGIN` based on measured probability bias
9. **Repeats** every 120 seconds during market hours (9 AM – 3:30 PM ET)

## Project structure

```
bot/
  main.py          # Entry point, main loop, cycle orchestration
  strategy.py      # Signal generation: parse strike, edge calc, market filters
  pricing.py       # Log-normal binary option pricer
  implied_vol.py   # IV back-out from market prices, adaptive safety margin
  kalshi_client.py # Kalshi REST API v2 client (RSA-PSS auth, buy/sell/cancel)
  price_feed.py    # BTC spot price + realized volatility (Kraken)
  risk.py          # Kelly sizing, drawdown guard, correlation discount
  store.py         # SQLite persistence, fill quality, calibration queries
  config.py        # All config loaded from environment variables
  monitor.py       # Slack/Discord webhook alerting

tests/
  test_pricing.py      # Log-normal model: ATM, expiry, monotonicity, edge cases
  test_strategy.py     # Strike parsing, spread filters, signal selection
  test_risk.py         # Kelly sizing, drawdown halt, correlation discount
  test_store.py        # SQLite persistence, calibration bias query
  test_implied_vol.py  # IV back-out round-trip, edge cases

docs/
  strategy.md      # Full strategy reference with formulas and parameter guide
```

## Setup

### Prerequisites
- Python 3.9+
- A [Kalshi](https://kalshi.com) account with API access (API key + RSA private key)

### Local

```bash
git clone https://github.com/utkarshp845/kalshi-trading-bot.git
cd kalshi-trading-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
python -m bot.main --dry-run   # test signals without placing orders
```

### Run tests

```bash
pytest tests/ -v
```

All 70 tests run without network access.

### EC2 (production)

Copy your secrets to the instance:

```bash
scp -i your-ec2-key.pem .env ubuntu@<EC2_IP>:/home/ubuntu/money-money/.env
scp -i your-ec2-key.pem bitcoin-key.pem ubuntu@<EC2_IP>:/home/ubuntu/money-money/bitcoin-key.pem
```

Start and enable the bot:

```bash
sudo systemctl start kalshi-bot
sudo systemctl status kalshi-bot
tail -f ~/money-money/logs/bot.log
```

## Configuration

Copy `.env.example` to `.env` and set the values.

### Strategy
| Variable | Default | Description |
|---|---|---|
| `MIN_EDGE` | `0.15` | Minimum net edge (after fees) to place a trade |
| `MIN_T_HOURS` | `1.0` | Skip markets expiring within this many hours |
| `VOL_SAFETY_MARGIN` | `1.25` | Static vol multiplier (used until IV calibration kicks in) |
| `MAX_VOL_RATIO` | `1.8` | Skip cycle when σ_7d/σ_30d exceeds this (unstable regime) |
| `MAX_BID_ASK_SPREAD` | `0.25` | Maximum absolute bid-ask spread |
| `MAX_BID_ASK_PCT_SPREAD` | `0.30` | Maximum spread as % of mid-price |
| `MAX_LAST_PRICE_DIVERGENCE` | `0.15` | Skip if last_price diverges >$0.15 from mid |

### Risk
| Variable | Default | Description |
|---|---|---|
| `MAX_DAILY_SPEND` | `5` | Hard cap on daily spend (USD) |
| `MAX_POSITIONS` | `2` | Max concurrent open positions |
| `KELLY_FRACTION` | `0.10` | Fraction of full Kelly to bet |
| `MAX_DRAWDOWN_PCT` | `0.20` | Stop trading if account drops 20% from session start |
| `BANKROLL_FRACTION` | `0.25` | Never risk >25% of actual balance per day |

### Implied Vol Calibration
| Variable | Default | Description |
|---|---|---|
| `IV_CALIBRATION_MIN_OBS` | `10` | Cycles before switching from static to adaptive margin |
| `IV_SAFETY_MARGIN_MIN` | `1.05` | Floor for adaptive vol margin |
| `IV_SAFETY_MARGIN_MAX` | `3.0` | Ceiling for adaptive vol margin |

### Position Management
| Variable | Default | Description |
|---|---|---|
| `ENABLE_POSITION_EXIT` | `true` | Exit losing positions mid-day |
| `EXIT_LOSS_TRIGGER` | `0.40` | Exit when value drops to 40% of entry price |
| `ENABLE_PRICE_IMPROVEMENT` | `true` | Try mid-price before paying full ask |
| `PRICE_IMPROVEMENT_TIMEOUT_SEC` | `45` | Wait this long for mid-price fill |

### Monitoring
| Variable | Default | Description |
|---|---|---|
| `ALERT_WEBHOOK_URL` | _(empty)_ | Slack/Discord webhook URL; empty = log-only |
| `DRY_RUN` | `false` | Log signals without placing real orders |

## CI/CD pipeline

Every push to `main` automatically deploys to EC2 via GitHub Actions:

1. SSH into the EC2 instance
2. `git pull origin main`
3. Reinstall Python dependencies
4. `sudo systemctl restart kalshi-bot`

Required GitHub secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`.

## Monitoring

```bash
# Live logs
tail -f ~/money-money/logs/bot.log

# Service status
sudo systemctl status kalshi-bot

# Today's trades
cat ~/money-money/logs/trades.csv

# Calibration check (requires sqlite3)
sqlite3 data/bot.db "SELECT run_at, iv_rv_ratio, adaptive_safety_margin FROM runs ORDER BY run_at DESC LIMIT 10;"

# Realized vs predicted edge (after 30+ trades)
sqlite3 data/bot.db "SELECT AVG(settled_value - theo_prob) AS prob_bias, COUNT(*) AS trades FROM orders WHERE settled_value IS NOT NULL;"
```
