import numpy as np
import pandas as pd
from pathlib import Path
import pandas_market_calendars as mcal

# settings 
CAPITAL = 100_000
COMMISSION = 0.0035      
MIN_COMMISSION = 0.35    
SLIPPAGE = 0.001         
TARGET_VOL = 0.02        
MAX_LEVERAGE = 4
LOOKBACK = 14            
TEST_START = "2023-10-09"
NY = "America/New_York"
STRATEGIES = {"Baseline": 1.0, "Partial gap": 0.5}

# data
def load_data():
    bars = pd.read_csv("data/spy_1min.csv", index_col="timestamp")
    bars.index = pd.to_datetime(bars.index, utc=True).tz_convert(NY)
    schedule = mcal.get_calendar("NYSE").schedule(
    start_date=bars.index.min().date(),
    end_date=bars.index.max().date()
    )
    close_by_day = {day.date(): close for day, close in schedule["market_close"].items()}
    bar_closes = pd.DatetimeIndex(
    [close_by_day[t.date()] for t in bars.index]
    )
    bars = bars[bars.index.tz_convert("UTC") < bar_closes]
    dividends = pd.read_csv("data/spy_dividends.csv")
    return bars, dividends


def add_indicators(bars, dividends):
    bars = bars.copy()
    local_time = bars.index.tz_localize(None)
    bars["date"] = local_time.strftime("%Y-%m-%d")
    bars["minute"] = local_time.hour * 60 + local_time.minute - 570   # 0 = 9:30
    bars["day_open"] = bars.groupby("date").open.transform("first")

    # VWAP with typical price (H+L+C)/3, it restarts every day
    typical = (bars.high + bars.low + bars.close) / 3
    bars["vwap"] = ((typical * bars.volume).groupby(bars.date).cumsum() / bars.volume.groupby(bars.date).cumsum())

    # sigma = average absolute move from open at the same minute over the previous 14 days 
    bars["move"] = (bars.close / bars.day_open - 1).abs()
    moves = bars.pivot(index="date", columns="minute", values="move")
    sigma = moves.shift(1).rolling(LOOKBACK).mean()
    bars = bars.join(sigma.stack(future_stack=True).rename("sigma"), on=["date", "minute"])

    days = bars.groupby("date").agg(open=("open", "first"), close=("close", "last"))
    days["exit_time"] = (
    pd.Series(bars.index, index=bars.index)
    .groupby(bars["date"])
    .last()
    + pd.Timedelta(minutes=1)
    )
    days["dividend"] = dividends.groupby("ex_dividend_date").cash_amount.sum()
    days["dividend"] = days.dividend.fillna(0)
    days["prev_close"] = days.close.shift(1) - days.dividend
    days["vol"] = days.close.pct_change().shift(2).rolling(14).std()
    bars["prev_close"] = bars.date.map(days.prev_close)

    # we trade every 30 minutes using the bar that just finished
    for col in ["close", "vwap", "sigma"]:
        bars["last_" + col] = bars.groupby("date")[col].shift(1)
    previous_time = pd.Series(bars.index, index=bars.index).groupby(bars.date).shift(1)
    contiguous = previous_time == bars.index - pd.Timedelta(minutes=1)
    checkpoints = bars[
    (bars.minute >= 30)
    & (bars.minute % 30 == 0)
    & contiguous
    & bars.last_sigma.notna()
    ]
    return bars, checkpoints, days


# signals
def noise_area(day_open, prev_close, sigma, gap_weight=1.0):
    # gap_weight = 1 is the paper
    # gap_weight = 0.5 is my version: only half of the overnight gap is used
    upper = day_open + gap_weight * np.maximum(prev_close - day_open, 0)
    lower = day_open - gap_weight * np.maximum(day_open - prev_close, 0)
    return upper * (1 + sigma), lower * (1 - sigma)


def get_signals(checkpoints, gap_weight):
    upper, lower = noise_area(checkpoints.day_open, checkpoints.prev_close, checkpoints.last_sigma, gap_weight)
    price, vwap = checkpoints.last_close, checkpoints.last_vwap
    long = (price > upper) & (price > vwap)
    short = (price < lower) & (price < vwap)
    return pd.Series(np.where(long, 1, np.where(short, -1, 0)), index=checkpoints.index)


# backtests
def trade(cash, qty, price):
    """Buy (qty > 0) or sell (qty < 0). Returns new cash and the cost paid."""
    fee = max(abs(qty) * COMMISSION, MIN_COMMISSION)
    cash = cash - qty * (price + np.sign(qty) * SLIPPAGE) - fee
    return cash, fee + abs(qty) * SLIPPAGE


def run_strategy(checkpoints, days, gap_weight, sizing, name):
    signals = checkpoints.assign(signal=get_signals(checkpoints, gap_weight))
    by_day = dict(list(signals.groupby("date")))
    cash = CAPITAL
    daily, trades = [], []

    for date, day in days.iloc[LOOKBACK:].iterrows():
        start = cash
        if sizing == "fixed":
            leverage = 1.0
        elif day.vol > 0:
            leverage = min(MAX_LEVERAGE, TARGET_VOL / day.vol)
        else:
            leverage = 0.0       # if there is no volatility estimate yet, I stay in cash
        shares = round(start / day.open * leverage)

        g = by_day.get(date, signals.iloc[:0])
        orders = list(zip(g.index, g.signal * shares, g.open))
        orders.append((day.exit_time, 0, day.close))   # flat at the close

        position, day_cost, day_trades = 0, 0.0, 0
        for time, target, price in orders:
            if target == position:
                continue
            if position != 0:        # exit (or first half of a reversal)
                cash, cost = trade(cash, -position, price)
                gross = position * (price - entry_price)
                trades.append({"strategy": name, "date": date, "side": "long" if position > 0 else "short",
                               "shares": abs(position), "entry_time": entry_time, "exit_time": time,
                               "gross_pnl": gross, "net_pnl": gross - entry_cost - cost})
                day_cost += cost
                day_trades += 1
                position = 0
            if target != 0:          # new entry
                cash, entry_cost = trade(cash, target, price)
                day_cost += entry_cost
                position, entry_price, entry_time = target, price, time

        daily.append({"strategy": name, "date": date, "equity": cash, "return": (cash - start) / start,
                      "trades": day_trades, "cost": day_cost, "leverage": leverage})
    return pd.DataFrame(daily), pd.DataFrame(trades)


def buy_and_hold(days):
    """Buy SPY at the open of the first day and hold until the close of the last day."""
    test_days = days.iloc[LOOKBACK:]
    price = days.close.iloc[LOOKBACK - 1]
    shares = int(CAPITAL // (price + COMMISSION + SLIPPAGE))
    cash, cost = trade(CAPITAL, shares, price)
    rows, prev_equity = [], CAPITAL
    for i, (date, day) in enumerate(test_days.iterrows()):
        cash += shares * day.dividend
        last = i == len(test_days) - 1
        if last:
            cash, exit_cost = trade(cash, -shares, day.close)
            cost += exit_cost
        equity = cash if last else cash + shares * day.close
        rows.append({"strategy": "SPY Buy & Hold", "date": date, "equity": equity,
                     "return": (equity - prev_equity) / prev_equity, "trades": int(last), "cost": cost})
        prev_equity, cost = equity, 0.0
    return pd.DataFrame(rows)


def authors_backtest(bars, days):
    """The authors' public Python code, shortened. Kept for comparison only:
    signal on the bar close, 13 of 14 days are enough for sigma,
    4x leverage when volatility is missing, commission but no slippage."""
    bars = bars.copy()
    bars.loc[bars.date == bars.date.iloc[0], ["move", "vwap"]] = np.nan   # they skip the first day
    move_mean = bars.groupby("minute").move.transform(lambda x: x.rolling(14, min_periods=13).mean())
    bars["sigma"] = move_mean.groupby(bars.minute).shift(1)

    equity, rows = CAPITAL, []
    for date, g in bars.groupby("date"):
        if g.sigma.isna().all():
            continue
        day = days.loc[date]
        upper = max(day.open, day.prev_close) * (1 + g.sigma)
        lower = min(day.open, day.prev_close) * (1 - g.sigma)
        signal = np.where((g.close > upper) & (g.close > g.vwap), 1.0,
                          np.where((g.close < lower) & (g.close < g.vwap), -1.0, 0.0))
        leverage = 4 if np.isnan(day.vol) else min(TARGET_VOL / day.vol, 4)
        shares = round(equity / day.open * leverage)
        exposure = pd.Series(np.where((g.minute + 1) % 30 == 0, signal, np.nan), index=g.index)
        exposure = exposure.ffill().shift(1).fillna(0)
        sides = np.abs(np.diff(np.append(exposure.values, 0))).sum()
        gross = (exposure * g.close.diff()).sum() * shares
        commission = sides * max(MIN_COMMISSION, COMMISSION * shares)
        net = gross - commission
        rows.append({"strategy": "Authors' code", "date": date, "equity": equity + net,
                     "return": net / equity, "trades": int(sides / 2), "cost": commission})
        equity += net
    return pd.DataFrame(rows)


# results
def metrics(daily, trades=None):
    r = daily["return"].to_numpy()
    wealth = np.r_[1.0, np.cumprod(1 + r)]
    vol = r.std(ddof=1) * np.sqrt(252)
    return {"total_return": wealth[-1] - 1,
            "annual_return": wealth[-1] ** (252 / len(r)) - 1,
            "annual_vol": vol,
            "sharpe": r.mean() * 252 / vol,
            "max_drawdown": -np.min(wealth / np.maximum.accumulate(wealth) - 1),
            "trades": int(daily.trades.sum()),
            "trade_hit_ratio": (trades.net_pnl > 0).mean() if trades is not None and len(trades) else np.nan,
            "total_cost": daily.cost.sum()}


def run_all():
    bars, dividends = load_data()
    bars, checkpoints, days = add_indicators(bars, dividends)
    daily, trades = [buy_and_hold(days)], []

    for name, weight in STRATEGIES.items():
        for sizing, label in [("fixed", "fixed 1x"), ("vol_target", "vol target")]:
            d, t = run_strategy(checkpoints, days, weight, sizing, f"{name} ({label})")
            daily.append(d)
            trades.append(t)
    authors = authors_backtest(bars, days)
    daily.append(authors[authors.date.isin(days.index[LOOKBACK:])])
    daily, trades = pd.concat(daily, ignore_index=True), pd.concat(trades, ignore_index=True)
    rows = []

    for (name, period), d in pd.concat([daily.assign(period=np.where(daily.date < TEST_START, "Development", "Test")),
                                        daily.assign(period="Full")]).groupby(["strategy", "period"], sort=False):
        t = trades[(trades.strategy == name) & trades.date.isin(d.date)]
        rows.append({"strategy": name, "period": period, "start": d.date.min(), "end": d.date.max(), **metrics(d, t)})
    performance = pd.DataFrame(rows)

    return bars, checkpoints, days, daily, trades, performance


if __name__ == "__main__":
    Path("results").mkdir(exist_ok=True)
    *_, daily, trades, performance = run_all()
    performance.to_csv("results/performance.csv", index=False)
    daily.to_csv("results/daily.csv", index=False)
    trades.to_csv("results/trades.csv", index=False)
    pd.set_option("display.width", 200)
    print(performance.round(4).to_string(index=False))
