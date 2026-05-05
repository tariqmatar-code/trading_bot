"""
=============================================================================
  RSI Oversold Scanner with Double-Bottom + Bullish Divergence (IG Markets)
=============================================================================
  Scans US stocks (S&P 500 by default) and finds candidates where:
    1. Among today's top losers (>= MIN_PCT_DROP %)
    2. Daily RSI <= 30 (oversold)
    3. RSI has touched the 30 line at least twice recently (double bottom)
    4. Bullish divergence: price made lower low, RSI made higher low

  Sends Telegram alerts. Reply "yes" within AUTO_BUY_TIMEOUT_SECONDS to
  auto-buy the signal via IG Markets.

  Uses yfinance (free, no API key) for market data scanning.
  Uses IG Markets for trade execution.
=============================================================================
"""

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import yfinance as yf

import config
from ig_client import IGClient
from utils import (
    log, alert, send_telegram,
    validate_config, test_ig_connection, test_telegram_connection,
)


# ---------------------------------------------------------------------------
# Stock universes
# ---------------------------------------------------------------------------
def get_universe() -> list[str]:
    if config.SCANNER_UNIVERSE == "SP500":
        return _fetch_sp500()
    elif config.SCANNER_UNIVERSE == "RUSSELL1000":
        return _fetch_russell1000()
    elif config.SCANNER_UNIVERSE == "ALL_US":
        return _fetch_all_us()
    else:
        log.warning(f"Unknown universe '{config.SCANNER_UNIVERSE}', defaulting to SP500")
        return _fetch_sp500()


def _fetch_sp500() -> list[str]:
    try:
        tables  = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        symbols = tables[0]["Symbol"].tolist()
        return [s.replace(".", "-") for s in symbols]
    except Exception as e:
        log.error(f"Failed to fetch S&P 500 list: {e}")
        return []


def _fetch_russell1000() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/Russell_1000_Index")
        for t in tables:
            cols = [c.lower() for c in t.columns.astype(str)]
            for col_name in ["ticker", "symbol"]:
                if col_name in cols:
                    idx = cols.index(col_name)
                    return [str(s).replace(".", "-") for s in t.iloc[:, idx].tolist()]
        log.warning("Could not find Russell 1000 ticker column, falling back to S&P 500")
        return _fetch_sp500()
    except Exception as e:
        log.error(f"Failed to fetch Russell 1000: {e}")
        return _fetch_sp500()


def _fetch_all_us() -> list[str]:
    log.warning("ALL_US scan is slow and may hit rate limits.")
    try:
        url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
        df  = pd.read_csv(url, sep="|")
        symbols = df["Symbol"].dropna().tolist()
        return [s for s in symbols if isinstance(s, str) and s.isalpha()][:5000]
    except Exception as e:
        log.error(f"Failed to fetch all US: {e}")
        return _fetch_sp500()


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def find_local_minima(series: pd.Series, window: int = 3) -> list[int]:
    minima = []
    for i in range(window, len(series) - window):
        local = series.iloc[i - window: i + window + 1]
        if series.iloc[i] == local.min() and pd.notna(series.iloc[i]):
            minima.append(i)
    return minima


def has_double_bottom_at_30(rsi: pd.Series, lookback: int = 60, threshold: float = 30) -> bool:
    recent  = rsi.tail(lookback).dropna()
    if len(recent) < 10:
        return False
    touches  = []
    in_touch = False
    for val in recent.values:
        if val <= threshold:
            if not in_touch:
                touches.append(val)
                in_touch = True
        else:
            if in_touch and val > threshold + 5:
                in_touch = False
    return len(touches) >= 2


def has_bullish_divergence(close: pd.Series, rsi: pd.Series, lookback: int = 30) -> bool:
    recent_close = close.tail(lookback)
    recent_rsi   = rsi.tail(lookback)
    if len(recent_close) < 10:
        return False
    price_minima = find_local_minima(recent_close, window=2)
    if len(price_minima) < 2:
        return False
    p1_idx, p2_idx = price_minima[-2], price_minima[-1]
    p1, p2 = recent_close.iloc[p1_idx], recent_close.iloc[p2_idx]
    r1, r2 = recent_rsi.iloc[p1_idx],  recent_rsi.iloc[p2_idx]
    if pd.isna(r1) or pd.isna(r2):
        return False
    return p2 < p1 and r2 > r1


# ---------------------------------------------------------------------------
# Scanning a single ticker
# ---------------------------------------------------------------------------
def analyze_ticker(symbol: str, timeframe: str) -> dict | None:
    try:
        if timeframe == "daily":
            df = yf.download(symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)
        elif timeframe == "hourly":
            df = yf.download(symbol, period="60d", interval="1h", progress=False, auto_adjust=True)
        else:
            return None

        if df.empty or len(df) < 30:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close  = df["Close"]
        volume = df["Volume"]

        if volume.tail(20).mean() < config.MIN_VOLUME:
            return None
        if len(close) < 2:
            return None

        pct_change = (close.iloc[-1] / close.iloc[-2] - 1) * 100
        if pct_change > -config.MIN_PCT_DROP:
            return None

        rsi         = compute_rsi(close)
        current_rsi = rsi.iloc[-1]
        if pd.isna(current_rsi) or current_rsi > config.RSI_OVERSOLD:
            return None
        if not has_double_bottom_at_30(rsi):
            return None
        if not has_bullish_divergence(close, rsi):
            return None

        return {
            "symbol":     symbol,
            "timeframe":  timeframe,
            "price":      float(close.iloc[-1]),
            "pct_change": float(pct_change),
            "rsi":        float(current_rsi),
            "volume":     int(volume.iloc[-1]),
        }
    except Exception as e:
        log.debug(f"Failed analyzing {symbol}: {e}")
        return None


def scan_universe(timeframe: str) -> list[dict]:
    tickers = get_universe()
    log.info(f"Scanning {len(tickers)} tickers on {timeframe} timeframe...")
    signals = []
    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            log.info(f"  Progress: {i}/{len(tickers)}, {len(signals)} signals so far")
        sig = analyze_ticker(ticker, timeframe)
        if sig:
            signals.append(sig)
            log.info(f"  Signal: {ticker} RSI={sig['rsi']:.1f} change={sig['pct_change']:.2f}%")
    return signals


# ---------------------------------------------------------------------------
# Auto-buy with Telegram approval
# ---------------------------------------------------------------------------
def get_telegram_updates(offset: int | None = None) -> list[dict]:
    try:
        url    = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN.strip()}/getUpdates"
        params = {"timeout": 5}
        if offset is not None:
            params["offset"] = offset
        return requests.get(url, params=params, timeout=10).json().get("result", [])
    except Exception as e:
        log.warning(f"Failed to get Telegram updates: {e}")
        return []


def wait_for_yes(timeout_seconds: int) -> bool:
    initial = get_telegram_updates()
    last_id = initial[-1]["update_id"] if initial else 0
    start   = time.time()
    while time.time() - start < timeout_seconds:
        updates = get_telegram_updates(offset=last_id + 1)
        for u in updates:
            last_id = u["update_id"]
            text = (u.get("message", {}).get("text") or "").strip().lower()
            if text in ("yes", "y", "buy", "ok"):
                return True
            if text in ("no", "n", "skip", "cancel"):
                return False
        time.sleep(2)
    return False


def auto_buy(client: IGClient, signal: dict) -> None:
    try:
        # Search for the IG epic matching this stock ticker
        epic = client.find_epic_for_ticker(signal["symbol"])
        if not epic:
            alert(f"⚠️ Could not find IG epic for {signal['symbol']}. Skipping auto-buy.", level="warning")
            return

        acc       = client.get_preferred_account()
        balance   = float(acc["balance"]["balance"])
        available = float(acc["balance"]["available"])
        currency  = acc["currency"]
        dollars   = min(balance * (config.SCANNER_POSITION_SIZE_PCT / 100), available)

        if dollars < 1:
            alert(f"⚠️ Not enough funds to buy {signal['symbol']}", level="warning")
            return

        size    = round(dollars / signal["price"], 2)
        confirm = client.open_position("BUY", epic, size)

        if confirm.get("dealStatus") == "ACCEPTED":
            alert(
                f"✅ <b>AUTO-BUY EXECUTED</b>\n"
                f"Symbol: {signal['symbol']} (epic: {epic})\n"
                f"Size: {size}\n"
                f"Price: ~{signal['price']:.2f}\n"
                f"Spent: {currency} {dollars:.2f}\n"
                f"Deal ID: {confirm.get('dealId', 'N/A')}"
            )
        else:
            alert(f"⚠️ Auto-buy rejected for {signal['symbol']}: {confirm.get('reason', 'unknown')}", level="warning")
    except Exception as e:
        alert(f"⚠️ Auto-buy failed for {signal['symbol']}: {e}", level="error")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def send_signals(signals: list[dict], timeframe: str) -> None:
    if not signals:
        send_telegram(f"🔍 <b>{timeframe.title()} scan complete</b> — no signals found.")
        return

    msg = f"🔍 <b>{timeframe.title()} RSI Scan — {len(signals)} signals</b>\n\n"
    for s in signals[:10]:
        msg += (
            f"• <b>{s['symbol']}</b> @ ${s['price']:.2f}\n"
            f"  Change: {s['pct_change']:+.2f}% | RSI: {s['rsi']:.1f}\n"
            f"  <a href='https://www.tradingview.com/chart/?symbol={s['symbol']}'>📊 Chart</a>\n\n"
        )
    if len(signals) > 10:
        msg += f"<i>...and {len(signals) - 10} more (see logs)</i>\n"
    send_telegram(msg)


def offer_auto_buy(client: IGClient, signals: list[dict]) -> None:
    if not signals:
        return
    top = signals[0]
    send_telegram(
        f"🤖 <b>Auto-buy available</b>\n"
        f"Top signal: <b>{top['symbol']}</b> @ ${top['price']:.2f}\n"
        f"Reply <b>yes</b> within {config.AUTO_BUY_TIMEOUT_SECONDS // 60} min "
        f"to buy at {config.SCANNER_POSITION_SIZE_PCT}% of balance.\n"
        f"Reply <b>no</b> to skip."
    )
    if wait_for_yes(config.AUTO_BUY_TIMEOUT_SECONDS):
        auto_buy(client, top)
    else:
        send_telegram("⏱ Auto-buy window closed. No action taken.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_scanner_loop() -> None:
    log.info("Running scanner startup checks...")
    validate_config()
    if not test_ig_connection():
        raise SystemExit(1)
    if not test_telegram_connection():
        raise SystemExit(1)

    client = IGClient()
    client.login()

    send_telegram(
        f"🔍 <b>RSI Scanner started (IG Markets)</b>\n"
        f"Universe: {config.SCANNER_UNIVERSE}\n"
        f"Schedule: hourly (1h timeframe) + end-of-day (daily timeframe)"
    )

    last_hourly    = None
    last_daily_date = None

    while True:
        try:
            now_utc  = datetime.now(timezone.utc)
            is_open  = client.is_market_open(config.IG_EPIC)

            if is_open:
                if last_hourly is None or (now_utc - last_hourly).total_seconds() >= 3600:
                    log.info("Running hourly 1h scan...")
                    signals = scan_universe("hourly")
                    send_signals(signals, "hourly")
                    if signals:
                        offer_auto_buy(client, signals)
                    last_hourly = now_utc

            if not is_open:
                today = now_utc.date()
                if last_daily_date != today:
                    log.info("Running end-of-day daily scan...")
                    signals = scan_universe("daily")
                    send_signals(signals, "daily")
                    if signals:
                        offer_auto_buy(client, signals)
                    last_daily_date = today

            time.sleep(60)
        except KeyboardInterrupt:
            send_telegram("🛑 <b>Scanner stopped manually</b>")
            break
        except Exception as e:
            log.error(f"Scanner error: {e}")
            send_telegram(f"⚠️ Scanner error: {e}")
            time.sleep(300)


if __name__ == "__main__":
    run_scanner_loop()
