"""Replay persisted cycle snapshots through the shared strategy engine."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import bot.config as cfg
from bot.feature_builder import build_market_features
from bot.kalshi_client import Market
from bot.models import AssetSnapshot, SourceSnapshot
from bot.portfolio_risk import PortfolioRisk
from bot.store import Store
from bot.strategy_engine import decide_signal


@dataclass
class ReplayMetrics:
    decisions: int = 0
    eligible: int = 0
    predicted_edge_sum: float = 0.0
    realized_edge_sum: float = 0.0
    capital_used: float = 0.0
    fills: int = 0


def _asset_from_row(row) -> AssetSnapshot:
    return AssetSnapshot(
        symbol=row["symbol"],
        series_ticker=row["series_ticker"],
        spot=row["spot"],
        sigma_short=row["sigma_short"],
        sigma_long=row["sigma_long"],
        sigma_adjusted=row["sigma_adjusted"],
        mu=row["mu"],
        iv_rv_ratio=row["iv_rv_ratio"],
        adaptive_margin=row["adaptive_margin"],
        spot_source=SourceSnapshot("kraken", row["symbol"], row["spot_fetched_at"], row["spot_freshness_sec"], row["spot_status"], "replay"),
        markets_source=SourceSnapshot("kalshi", row["symbol"], row["markets_fetched_at"], row["markets_freshness_sec"], row["markets_status"], "replay"),
        iv_source=SourceSnapshot("deribit", row["symbol"], row["iv_fetched_at"], row["iv_freshness_sec"], row["iv_status"], "replay"),
        degraded=bool(row["degraded"]),
        health_status=row["health_status"],
        open_positions=row["open_positions"] or 0,
    )


def _markets_from_rows(rows) -> list[Market]:
    markets: list[Market] = []
    for row in rows:
        markets.append(Market(
            ticker=row["ticker"],
            event_ticker=f"KX{row['symbol']}",
            status="open",
            close_time=row["close_time"],
            yes_ask=row["yes_ask"],
            yes_bid=row["yes_bid"],
            no_ask=row["no_ask"],
            no_bid=row["no_bid"],
            last_price=None,
        ))
    return markets


def replay(store: Store, date_from: str, date_to: str, symbols: list[str]) -> str:
    asset_rows = store.get_asset_runs_in_range(date_from, date_to, symbols)
    rows_by_cycle: dict[str, list] = defaultdict(list)
    for row in asset_rows:
        rows_by_cycle[row["cycle_id"]].append(row)

    metrics_by_symbol: dict[str, ReplayMetrics] = defaultdict(ReplayMetrics)
    combined = ReplayMetrics()
    risk = PortfolioRisk(
        daily_spend_pct=cfg.DAILY_SPEND_PCT,
        daily_spend_floor=cfg.DAILY_SPEND_FLOOR,
        max_contracts_per_market=cfg.MAX_CONTRACTS_PER_MARKET,
        max_positions=cfg.MAX_POSITIONS,
        max_symbol_daily_spend_pct=cfg.MAX_SYMBOL_DAILY_SPEND_PCT,
        max_symbol_positions=cfg.MAX_SYMBOL_POSITIONS,
        kelly_fraction=cfg.KELLY_FRACTION,
        max_drawdown_pct=cfg.MAX_DRAWDOWN_PCT,
        bankroll_fraction=cfg.BANKROLL_FRACTION,
        drawdown_tier_1_pct=cfg.DRAWDOWN_TIER_1_PCT,
        drawdown_tier_1_scale=cfg.DRAWDOWN_TIER_1_SCALE,
        drawdown_tier_2_pct=cfg.DRAWDOWN_TIER_2_PCT,
        drawdown_tier_2_scale=cfg.DRAWDOWN_TIER_2_SCALE,
    )
    risk.set_session_balance(100.0)
    open_positions_by_symbol: dict[str, int] = {}

    for cycle_id in sorted(rows_by_cycle):
        for asset_row in rows_by_cycle[cycle_id]:
            asset = _asset_from_row(asset_row)
            market_rows = store.get_market_snapshots_for_cycle(cycle_id, asset.symbol)
            features = build_market_features(asset, _markets_from_rows(market_rows), cfg.KALSHI_TAKER_FEE)
            for feature in features:
                decision = decide_signal(store, asset, feature, held_tickers=set(), before_iso=cycle_id)
                symbol_metrics = metrics_by_symbol[asset.symbol]
                symbol_metrics.decisions += 1
                combined.decisions += 1
                if not decision.eligible:
                    continue
                symbol_metrics.eligible += 1
                combined.eligible += 1
                symbol_metrics.predicted_edge_sum += decision.edge
                combined.predicted_edge_sum += decision.edge
                contracts = risk.size_order(decision, current_balance=100.0, open_positions_by_symbol=open_positions_by_symbol)
                if contracts > 0:
                    cost = contracts * decision.ask
                    symbol_metrics.capital_used += cost
                    combined.capital_used += cost
                    open_positions_by_symbol[decision.symbol] = open_positions_by_symbol.get(decision.symbol, 0) + 1

    lines = [f"Replay {date_from} -> {date_to} ({','.join(symbols)})"]
    for symbol in symbols:
        m = metrics_by_symbol[symbol]
        avg_pred = m.predicted_edge_sum / m.eligible if m.eligible else 0.0
        lines.append(
            f"{symbol}: decisions={m.decisions} eligible={m.eligible} "
            f"avg_pred_edge={avg_pred:.4f} capital_utilization=${m.capital_used:.2f}"
        )
    combined_avg_pred = combined.predicted_edge_sum / combined.eligible if combined.eligible else 0.0
    lines.append(
        f"COMBINED: decisions={combined.decisions} eligible={combined.eligible} "
        f"avg_pred_edge={combined_avg_pred:.4f} capital_utilization=${combined.capital_used:.2f}"
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Replay stored cycle snapshots.")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--db", default=None)
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db) if args.db else cfg.DB_PATH
    store = Store(db_path=db_path, trades_csv_path=db_path.parent / ".replay_noop.csv")
    store.open()
    try:
        symbols = [sym.strip().upper() for sym in args.symbols.split(",") if sym.strip()]
        print(replay(store, args.date_from, args.date_to, symbols))
    finally:
        store.close()


if __name__ == "__main__":
    main()
