"""Test end-to-end con broker, dati e database SIMULATI (nessuna chiamata di rete)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from alpaca.trading.enums import AssetClass, OrderClass, OrderSide, OrderStatus, OrderType, PositionSide

from config.settings import Settings
from src.execution.executor import OrderExecutor
from src.execution.risk_manager import RiskManager
from src.main import Services, SessionStats, trading_cycle
from src.strategy.selector import StrategySelector

_ids = itertools.count(1)


class FakeTrading:
    """Broker minimale: gli ordini limit marketable vengono eseguiti subito al limite."""

    def __init__(self, positions=None, closed_orders=None):
        self.positions = positions or []
        self.orders: dict[str, SimpleNamespace] = {}
        self.closed_orders = closed_orders or []
        self.submitted: list = []
        self.replaced: list = []

    def get_account(self):
        return SimpleNamespace(equity="100000", cash="100000", last_equity="100000", buying_power="200000",
                               long_market_value="0", short_market_value="0", status=None,
                               shorting_enabled=True, pattern_day_trader=False)

    def get_all_positions(self):
        return self.positions

    def get_asset(self, symbol):
        return SimpleNamespace(tradable=True, shortable=True, easy_to_borrow=True)

    def get_orders(self, req):
        if str(getattr(req.status, "value", req.status)) == "closed":
            return self.closed_orders
        live = [o for o in self.orders.values() if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELED)]
        return [o for o in live if not req.symbols or o.symbol in req.symbols]

    def submit_order(self, req):
        oid = f"o{next(_ids)}"
        self.submitted.append(req)
        if req.order_class == OrderClass.OCO:
            stop_leg = SimpleNamespace(id=f"{oid}-sl", symbol=req.symbol, status=OrderStatus.NEW, order_type=OrderType.STOP,
                                       type=OrderType.STOP, stop_price=req.stop_loss.stop_price, limit_price=None,
                                       order_class=OrderClass.OCO, legs=None)
            order = SimpleNamespace(id=oid, symbol=req.symbol, status=OrderStatus.NEW, order_type=OrderType.LIMIT,
                                    type=OrderType.LIMIT, stop_price=None, limit_price=req.take_profit.limit_price,
                                    order_class=OrderClass.OCO, legs=[stop_leg], filled_qty="0", filled_avg_price=None)
        else:
            filled = req.type == OrderType.LIMIT
            order = SimpleNamespace(id=oid, symbol=req.symbol, side=req.side, qty=req.qty,
                                    status=OrderStatus.FILLED if filled else OrderStatus.NEW,
                                    order_type=req.type, type=req.type, stop_price=getattr(req, "stop_price", None),
                                    limit_price=getattr(req, "limit_price", None), order_class=OrderClass.SIMPLE, legs=None,
                                    filled_qty=str(req.qty) if filled else "0",
                                    filled_avg_price=str(req.limit_price) if filled else None)
        self.orders[oid] = order
        return order

    def get_order_by_id(self, oid):
        return self.orders[str(oid)]

    def replace_order_by_id(self, oid, req):
        self.replaced.append((oid, req))
        return SimpleNamespace(id=f"{oid}-r", stop_price=req.stop_price)

    def cancel_order_by_id(self, oid):
        if str(oid) in self.orders:
            self.orders[str(oid)].status = OrderStatus.CANCELED


class FakeStorage:
    enabled = True
    error_count = 0

    def __init__(self, open_trades=None):
        self.open_trades = open_trades or []
        self.inserted, self.updated, self.closed, self.screening = [], [], [], []

    def get_open_trades(self):
        return self.open_trades

    def get_recent_stop_tickers(self, since):
        return []

    def insert_trade(self, row):
        self.inserted.append(row)
        return 1

    def update_trade(self, trade_id, fields):
        self.updated.append((trade_id, fields))

    def close_trade(self, trade_id, exit_price, pnl, pnl_pct, reason):
        self.closed.append((trade_id, exit_price, pnl, reason))

    def save_screening_logs(self, rows, run_id):
        self.screening = rows

    def save_equity_snapshot(self, *a, **k):
        pass

    def log_run(self, *a, **k):
        pass


class FakeMarket:
    """Un titolo in breakout (BRK), uno laterale e calmo (FLAT)."""

    volumes_reliable = True

    def __init__(self, prices):
        self.prices = prices
        rng = np.random.default_rng(7)
        idx = pd.date_range("2025-06-02", periods=300, freq="B", tz="UTC")
        up = 50 + np.cumsum(0.25 + rng.normal(0, 0.05, 300))
        flat = 100 + rng.normal(0, 0.01, 300)
        self.bars = {
            "BRK": pd.DataFrame({"open": up, "high": up + 0.5, "low": up - 0.5, "close": up, "volume": 5e6}, index=idx),
            "FLAT": pd.DataFrame({"open": flat, "high": flat + 0.05, "low": flat - 0.05, "close": flat, "volume": 5e6}, index=idx),
        }

    def get_daily_bars(self, symbols):
        return {s: self.bars[s] for s in symbols if s in self.bars}

    def get_reference_prices(self, symbols):
        return {s: self.prices[s] for s in symbols if s in self.prices}


def settings(**kw) -> Settings:
    base = dict(_env_file=None, ALPACA_API_KEY="x", ALPACA_SECRET_KEY="y", WATCHLIST="BRK,FLAT",
                CHASE_INTERVAL_SEC=2, ORDER_POLL_SEC=0.01, MIN_OPPORTUNITY_SCORE=0)
    base.update(kw)
    return Settings(**base)


def services(s: Settings, trading: FakeTrading, storage: FakeStorage, market: FakeMarket) -> Services:
    executor = OrderExecutor(trading, s)
    return Services(trading=trading, market=market, executor=executor,
                    risk=RiskManager(s, trading, market, executor, storage),
                    selector=StrategySelector(s), storage=storage)


def breakout_price(market: FakeMarket) -> float:
    high20 = float(market.bars["BRK"]["high"].tail(20).max())
    return round(high20 + 0.1, 2)


def test_dry_run_cycle_sends_no_orders():
    market = FakeMarket({})
    market.prices = {"BRK": breakout_price(market), "FLAT": 100.0}
    s, trading, storage = settings(DRY_RUN=True), FakeTrading(), FakeStorage()
    summary = trading_cycle(s, services(s, trading, storage, market), "run1", 100_000, 200_000, SessionStats())
    rows = {r["ticker"]: r for r in storage.screening}
    assert rows["BRK"]["strategy"] == "BREAKOUT_LONG" and rows["BRK"]["selected"]
    assert "DRY_RUN" in rows["BRK"]["reason"]
    assert rows["FLAT"]["strategy"] == "NO_TRADE"
    assert trading.submitted == [] and storage.inserted == []
    assert "ordini simulati 1" in summary


def test_live_cycle_opens_and_protects():
    market = FakeMarket({})
    price = breakout_price(market)
    market.prices = {"BRK": price, "FLAT": 100.0}
    s, trading, storage = settings(DRY_RUN=False), FakeTrading(), FakeStorage()
    trading_cycle(s, services(s, trading, storage, market), "run1", 100_000, 200_000, SessionStats())
    entry, oco = trading.submitted
    assert entry.side == OrderSide.BUY and entry.limit_price == pytest.approx(price)
    assert oco.order_class == OrderClass.OCO and oco.side == OrderSide.SELL and oco.qty == entry.qty
    row = storage.inserted[0]
    assert row["side"] == "LONG" and row["stop_price"] < row["entry_price"] < row["take_profit"]
    assert row["details_json"]["protection"] == "OCO"
    # Rischio allo stop entro l'1% dell'equity
    assert (row["entry_price"] - row["stop_price"]) * row["qty"] <= 1_000 + 1e-6


def _position(symbol, qty, entry, price, side=PositionSide.LONG):
    return SimpleNamespace(symbol=symbol, qty=str(qty), avg_entry_price=str(entry), current_price=str(price),
                           market_value=str(qty * price), side=side, asset_class=AssetClass.US_EQUITY)


def test_risk_manager_restores_protection_and_moves_stop():
    market = FakeMarket({"BRK": 104.5})
    trade = {"id": 7, "ticker": "BRK", "strategy": "BREAKOUT_LONG", "side": "LONG", "entry_price": 100, "qty": 10,
             "initial_stop": 96, "stop_price": 96, "take_profit": 112, "risk_per_share": 4,
             "created_at": datetime.now(timezone.utc).isoformat(), "details_json": {"atr": 2, "max_holding_days": 30}}
    s = settings(DRY_RUN=False)
    trading, storage = FakeTrading(positions=[_position("BRK", 10, 100, 104.5)]), FakeStorage([trade])
    risk = services(s, trading, storage, market).risk
    report = risk.manage_open_positions(allow_dynamic=True)
    assert report.open_count == 1
    oco = trading.submitted[0]  # protezione ricreata con stop e target salvati
    assert oco.order_class == OrderClass.OCO and oco.stop_loss.stop_price == 96 and oco.take_profit.limit_price == 112
    # +1.1R -> stop a breakeven tramite replace della gamba stop
    (oid, req), = trading.replaced
    assert oid.endswith("-sl") and req.stop_price == 100.0
    assert any(f.get("stop_price") == 100.0 for _, f in storage.updated)


def test_time_stop_closes_position():
    market = FakeMarket({"BRK": 101.0})
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    trade = {"id": 8, "ticker": "BRK", "strategy": "PULLBACK_LONG", "side": "LONG", "entry_price": 100, "qty": 10,
             "initial_stop": 97, "stop_price": 97, "take_profit": 109, "risk_per_share": 3,
             "created_at": old, "details_json": {"atr": 2, "max_holding_days": 30}}
    s = settings(DRY_RUN=False)
    trading, storage = FakeTrading(positions=[_position("BRK", 10, 100, 101.0)]), FakeStorage([trade])
    report = services(s, trading, storage, market).risk.manage_open_positions(allow_dynamic=True)
    assert report.open_count == 0
    (tid, exit_price, pnl, reason), = storage.closed
    assert tid == 8 and reason == "TIME_STOP" and exit_price == pytest.approx(101.0) and pnl == pytest.approx(10.0)
    assert trading.submitted[0].side == OrderSide.SELL


def test_reconcile_trade_closed_by_broker_stop():
    created = datetime.now(timezone.utc) - timedelta(days=2)
    stop_fill = SimpleNamespace(id="x1", symbol="BRK", side=OrderSide.SELL, status=OrderStatus.FILLED, filled_avg_price="95.9",
                                order_type=OrderType.STOP, type=OrderType.STOP, client_order_id="stk-oco-abc",
                                filled_at=created + timedelta(days=1), updated_at=None, legs=None)
    trade = {"id": 9, "ticker": "BRK", "strategy": "BREAKOUT_LONG", "side": "LONG", "entry_price": 100, "qty": 10,
             "created_at": created.isoformat(), "details_json": {}}
    s = settings(DRY_RUN=False)
    trading, storage = FakeTrading(closed_orders=[stop_fill]), FakeStorage([trade])
    services(s, trading, storage, FakeMarket({})).risk.manage_open_positions()
    (tid, exit_price, pnl, reason), = storage.closed
    assert (tid, reason) == (9, "STOP_LOSS") and exit_price == pytest.approx(95.9) and pnl == pytest.approx(-41.0)


def test_daily_bars_cache_downloads_once_per_day():
    from src.data.market_data import MarketDataService

    idx = pd.MultiIndex.from_product([["AAA"], pd.date_range("2025-01-02", periods=5, freq="B", tz="UTC")])
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    calls = []

    class Client:
        def get_stock_bars(self, req):
            calls.append(req.symbol_or_symbols)
            return SimpleNamespace(df=df)

    svc = MarketDataService(settings())
    svc.stock_client = Client()
    assert list(svc.get_daily_bars(["AAA", "BBB"])) == ["AAA"]
    assert list(svc.get_daily_bars(["AAA", "BBB"])) == ["AAA"]  # tutto dalla cache, BBB non riprovato
    assert calls == [["AAA", "BBB"]]
