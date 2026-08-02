"""Next-bar-open portfolio simulator with order-level audit output."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from alpha.execution import OrderRequest, OrderStatus, PaperBroker, RiskLimits


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    max_gross_leverage: float = 1.0


@dataclass
class BacktestResult:
    summary: dict[str, float]
    equity: pd.DataFrame
    weights: pd.DataFrame
    orders: pd.DataFrame
    config: BacktestConfig

    def write(self, data_dir: str | Path, run_id: str) -> Path:
        root = Path(data_dir) / "backtests" / run_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "summary.json").write_text(json.dumps(self.summary, indent=2), encoding="utf-8")
        self.equity.to_parquet(root / "equity.parquet", index=False)
        self.weights.to_parquet(root / "weights.parquet", index=False)
        self.orders.to_parquet(root / "orders.parquet", index=False)
        (root / "config.json").write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
        return root


def run_backtest(
    bars: pd.DataFrame, target_weights: pd.DataFrame, config: BacktestConfig | None = None
) -> BacktestResult:
    """Simulate close-derived signals filling at each instrument's next bar open."""
    config = config or BacktestConfig()
    need_bars = {"ts_event_ms", "instrument_id", "open", "close"}
    need_weights = {"ts_event_ms", "instrument_id", "target_weight"}
    if missing := need_bars - set(bars.columns):
        raise ValueError(f"bars 缺少字段: {sorted(missing)}")
    if missing := need_weights - set(target_weights.columns):
        raise ValueError(f"target_weights 缺少字段: {sorted(missing)}")

    bars = bars.sort_values(["ts_event_ms", "instrument_id"]).copy()
    signals = target_weights.sort_values(["instrument_id", "ts_event_ms"]).copy()
    next_ts = bars.groupby("instrument_id", sort=False)["ts_event_ms"].shift(-1)
    signal_to_fill = bars[["ts_event_ms", "instrument_id"]].copy()
    signal_to_fill["ts_fill_ms"] = next_ts
    executions = signals.merge(signal_to_fill, on=["ts_event_ms", "instrument_id"], how="left")
    unfillable = executions[executions["ts_fill_ms"].isna()]
    executions = executions.dropna(subset=["ts_fill_ms"])
    executions["ts_fill_ms"] = executions["ts_fill_ms"].astype("int64")
    by_fill = {
        key: group.set_index("instrument_id")
        for key, group in executions.groupby("ts_fill_ms", sort=False)
    }
    broker = PaperBroker(RiskLimits(config.max_gross_leverage))
    cash = config.initial_cash
    units: dict[str, float] = {}
    last_close: dict[str, float] = {}
    latest_target: dict[str, float] = {}
    order_rows: list[dict] = []
    equity_rows: list[dict] = []
    weight_rows: list[dict] = []
    fee_rate = config.fee_bps / 10_000
    slip_rate = config.slippage_bps / 10_000

    for _, signal in unfillable.iterrows():
        order_rows.append({
            "ts_signal_ms": int(signal["ts_event_ms"]), "ts_fill_ms": None,
            "instrument_id": signal["instrument_id"], "side": None,
            "target_weight": float(signal["target_weight"]), "quantity": 0.0,
            "reference_price": None, "fill_price": None, "notional": 0.0, "fee": 0.0,
            "slippage_cost": 0.0, "reason": "no_next_bar", "status": OrderStatus.SKIPPED_NO_PRICE.value,
        })

    for ts, day in bars.groupby("ts_event_ms", sort=True):
        day = day.set_index("instrument_id")
        for instrument, row in day.iterrows():
            last_close[instrument] = float(row["close"])
        equity_before = cash + sum(units.get(i, 0.0) * px for i, px in last_close.items())
        requests = by_fill.get(int(ts))
        if requests is not None:
            gross = float(requests["target_weight"].abs().sum())
            for instrument, signal in requests.iterrows():
                target = float(signal["target_weight"])
                latest_target[instrument] = target
                open_px = float(day.loc[instrument, "open"]) if instrument in day.index else 0.0
                current_units = units.get(instrument, 0.0)
                desired_notional = target * equity_before
                quantity = desired_notional / open_px - current_units if open_px > 0 else 0.0
                request = OrderRequest(
                    ts_signal_ms=int(signal["ts_event_ms"]), ts_fill_ms=int(ts),
                    instrument_id=instrument, target_weight=target, quantity=quantity,
                    reference_price=open_px,
                )
                status = broker.validate(request)
                if gross > config.max_gross_leverage:
                    status = OrderStatus.REJECTED
                fill_px = open_px * (1 + slip_rate if quantity > 0 else 1 - slip_rate)
                notional = quantity * fill_px
                fee = abs(notional) * fee_rate if status == OrderStatus.FILLED else 0.0
                if status == OrderStatus.FILLED:
                    units[instrument] = current_units + quantity
                    cash -= notional + fee
                order_rows.append({
                    "ts_signal_ms": int(signal["ts_event_ms"]), "ts_fill_ms": int(ts),
                    "instrument_id": instrument, "side": "buy" if quantity >= 0 else "sell",
                    "target_weight": target, "quantity": quantity, "reference_price": open_px,
                    "fill_price": fill_px, "notional": notional, "fee": fee,
                    "slippage_cost": abs(quantity) * abs(fill_px - open_px),
                    "reason": "rebalance", "status": status.value,
                })
        equity = cash + sum(units.get(i, 0.0) * px for i, px in last_close.items())
        equity_rows.append({"ts_event_ms": int(ts), "equity": equity, "cash": cash})
        for instrument, quantity in units.items():
            price = last_close.get(instrument)
            if price is not None:
                weight_rows.append({"ts_event_ms": int(ts), "instrument_id": instrument,
                                    "target_weight": latest_target.get(instrument, 0.0),
                                    "actual_weight": quantity * price / equity if equity else 0.0})

    equity_frame = pd.DataFrame(equity_rows)
    equity_frame["return"] = equity_frame["equity"].pct_change().fillna(0.0)
    total_return = equity_frame["equity"].iloc[-1] / config.initial_cash - 1 if len(equity_frame) else 0.0
    drawdown = equity_frame["equity"] / equity_frame["equity"].cummax() - 1 if len(equity_frame) else pd.Series([0.0])
    periodic_returns = equity_frame["return"]
    period_ms = equity_frame["ts_event_ms"].diff().median() if len(equity_frame) > 1 else None
    annual_periods = 365 * 86_400_000 / period_ms if period_ms and period_ms > 0 else 1.0
    volatility = float(periodic_returns.std(ddof=0) * annual_periods**0.5)
    sharpe = float(periodic_returns.mean() / periodic_returns.std(ddof=0) * annual_periods**0.5) if periodic_returns.std(ddof=0) else 0.0
    summary = {"initial_cash": config.initial_cash, "final_equity": float(equity_frame["equity"].iloc[-1]) if len(equity_frame) else config.initial_cash,
               "total_return": float(total_return), "annualized_volatility": volatility,
               "sharpe": sharpe, "max_drawdown": float(drawdown.min()),
               "order_count": float(len(order_rows)), "total_fees": float(sum(r["fee"] for r in order_rows))}
    return BacktestResult(
        summary, equity_frame, pd.DataFrame(weight_rows), pd.DataFrame(order_rows), config
    )
