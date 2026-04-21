"""Replay persisted cycle snapshots through the shared strategy engine."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import bot.config as cfg
from bot.models import AssetSnapshot, MarketFeature, SourceSnapshot
from bot.portfolio_risk import PortfolioRisk
from bot.store import Store
from bot.strategy_engine import decide_signal


@dataclass
class ReplayMetrics:
    decisions: int = 0
    eligible: int = 0
    labeled_eligible: int = 0
    predicted_edge_sum: float = 0.0
    realized_edge_sum: float = 0.0
    capital_used: float = 0.0
    fills: int = 0
    wins: int = 0
    maker_attempts: int = 0
    maker_fills: int = 0
    cancels: int = 0
    max_drawdown: float = 0.0


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


def _feature_from_row(row) -> MarketFeature:
    return MarketFeature(
        symbol=row["symbol"],
        ticker=row["ticker"],
        close_time=row["close_time"],
        expiry_bucket=row["expiry_bucket"],
        strike=row["strike"],
        side=row["side"],
        contract_theo_prob=row["contract_theo_prob"],
        yes_theo_prob=row["yes_theo_prob"],
        ask=row["ask"],
        bid=row["bid"],
        mid=row["mid"],
        yes_bid=row["yes_bid"],
        yes_ask=row["yes_ask"],
        no_bid=row["no_bid"],
        no_ask=row["no_ask"],
        spread_abs=row["spread_abs"],
        spread_pct=row["spread_pct"],
        gross_edge=row["gross_edge"],
        edge=row["edge"],
        fee=row["fee"],
        hours_to_expiry=row["hours_to_expiry"],
        distance_from_spot_sigma=row["distance_from_spot_sigma"],
        last_price_divergence=row["last_price_divergence"],
        chain_break_ratio=row["chain_break_ratio"],
        chain_ok=bool(row["chain_ok"]),
        enough_sane_strikes=bool(row["enough_sane_strikes"]),
        spread_ok=bool(row["spread_ok"]),
        last_price_ok=bool(row["last_price_ok"]),
        top_of_book_size=float(row["top_of_book_size"] or 0.0),
        resting_size_at_entry=float(row["resting_size_at_entry"] or 0.0),
        cumulative_size_at_entry=float(row["cumulative_size_at_entry"] or 0.0),
        expected_fill_price=row["expected_fill_price"],
        depth_slippage=float(row["depth_slippage"] or 0.0),
        orderbook_imbalance=float(row["orderbook_imbalance"] or 0.0),
        orderbook_available=bool(row["orderbook_available"]),
    )


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
    balance = 100.0
    peak_balance = balance
    outcome_rows = store.get_market_outcomes_for_tickers(
        [row["ticker"] for rows in rows_by_cycle.values() for asset_row in rows for row in store.get_market_snapshots_for_cycle(asset_row["cycle_id"], asset_row["symbol"])]
    )
    execution_attempts = store.get_execution_attempts_in_range(date_from, date_to)
    execution_by_cycle_ticker: dict[tuple[str, str], list] = defaultdict(list)
    for row in execution_attempts:
        execution_by_cycle_ticker[(str(row["cycle_id"]), str(row["ticker"]))].append(row)

    for cycle_id in sorted(rows_by_cycle):
        for asset_row in rows_by_cycle[cycle_id]:
            asset = _asset_from_row(asset_row)
            market_rows = store.get_market_snapshots_for_cycle(cycle_id, asset.symbol)
            for feature_row in market_rows:
                feature = _feature_from_row(feature_row)
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
                contracts = risk.size_order(decision, current_balance=balance, open_positions_by_symbol=open_positions_by_symbol)
                if contracts > 0:
                    cost = contracts * decision.ask
                    symbol_metrics.capital_used += cost
                    combined.capital_used += cost
                    open_positions_by_symbol[decision.symbol] = open_positions_by_symbol.get(decision.symbol, 0) + 1
                    outcome = outcome_rows.get(decision.ticker)
                    if outcome is not None and outcome["settlement_value"] is not None:
                        realized_edge = float(outcome["settlement_value"]) - decision.ask - decision.fee
                        symbol_metrics.labeled_eligible += 1
                        combined.labeled_eligible += 1
                        symbol_metrics.realized_edge_sum += realized_edge
                        combined.realized_edge_sum += realized_edge
                        if float(outcome["settlement_value"]) >= 0.5:
                            symbol_metrics.wins += 1
                            combined.wins += 1
                        pnl = realized_edge * contracts
                        balance += pnl
                        peak_balance = max(peak_balance, balance)
                        drawdown = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0.0
                        symbol_metrics.max_drawdown = max(symbol_metrics.max_drawdown, drawdown)
                        combined.max_drawdown = max(combined.max_drawdown, drawdown)
                attempts = execution_by_cycle_ticker.get((cycle_id, decision.ticker), [])
                for attempt in attempts:
                    status = str(attempt["status"] or "")
                    if status in {"live_fill", "live_no_fill", "no_fill", "error"}:
                        symbol_metrics.maker_attempts += 1
                        combined.maker_attempts += 1
                        if status == "live_fill":
                            symbol_metrics.maker_fills += 1
                            combined.maker_fills += 1
                        if status in {"no_fill", "live_no_fill"}:
                            symbol_metrics.cancels += 1
                            combined.cancels += 1

    lines = [f"Replay {date_from} -> {date_to} ({','.join(symbols)})"]
    for symbol in symbols:
        m = metrics_by_symbol[symbol]
        avg_pred = m.predicted_edge_sum / m.eligible if m.eligible else 0.0
        avg_realized = m.realized_edge_sum / m.labeled_eligible if m.labeled_eligible else 0.0
        win_rate = m.wins / m.labeled_eligible if m.labeled_eligible else 0.0
        maker_fill_rate = m.maker_fills / m.maker_attempts if m.maker_attempts else 0.0
        cancel_rate = m.cancels / m.maker_attempts if m.maker_attempts else 0.0
        lines.append(
            f"{symbol}: decisions={m.decisions} eligible={m.eligible} "
            f"labeled={m.labeled_eligible} avg_pred_edge={avg_pred:.4f} "
            f"avg_realized_edge={avg_realized:.4f} win_rate={win_rate:.2%} "
            f"maker_fill_rate={maker_fill_rate:.2%} cancel_rate={cancel_rate:.2%} "
            f"capital_utilization=${m.capital_used:.2f} max_drawdown={m.max_drawdown:.2%}"
        )
    combined_avg_pred = combined.predicted_edge_sum / combined.eligible if combined.eligible else 0.0
    combined_avg_realized = combined.realized_edge_sum / combined.labeled_eligible if combined.labeled_eligible else 0.0
    combined_win_rate = combined.wins / combined.labeled_eligible if combined.labeled_eligible else 0.0
    combined_maker_fill_rate = combined.maker_fills / combined.maker_attempts if combined.maker_attempts else 0.0
    combined_cancel_rate = combined.cancels / combined.maker_attempts if combined.maker_attempts else 0.0
    lines.append(
        f"COMBINED: decisions={combined.decisions} eligible={combined.eligible} "
        f"labeled={combined.labeled_eligible} avg_pred_edge={combined_avg_pred:.4f} "
        f"avg_realized_edge={combined_avg_realized:.4f} win_rate={combined_win_rate:.2%} "
        f"maker_fill_rate={combined_maker_fill_rate:.2%} cancel_rate={combined_cancel_rate:.2%} "
        f"capital_utilization=${combined.capital_used:.2f} max_drawdown={combined.max_drawdown:.2%}"
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
