"""
=============================================================================
  Trading Bot - MA Crossover (IG Markets)
=============================================================================
  - Trades on MA crossover signals via IG Markets REST API
  - Sends Telegram alert on every buy/sell
  - Sends market open/close reports
  - Only trades during market hours (9:30 AM - 4:00 PM ET weekdays)
=============================================================================
"""

import time
import traceback
from datetime import datetime

import pandas as pd
import yfinance as yf
import pytz

import config
from ig_client import IGClient
from utils import (
    log, alert, send_telegram,
    validate_config, test_ig_connection, test_telegram_connection,
)
from reports import generate_report

ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def startup_checks() -> IGClient:
    log.info("Running startup checks...")
    validate_config()
    if not test_ig_connection():
        raise SystemExit(1)
    if not test_telegram_connection():
        raise SystemExit(1)

    client = IGClient()
    client.login()
    return client


# ---------------------------------------------------------------------------
# Market hours check (US market: Mon-Fri 9:30 AM – 4:00 PM ET)
# ---------------------------------------------------------------------------
def is_us_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def seconds_until_market_open() -> int:
    now = datetime.now(ET)
    # Find next weekday 9:30 AM
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= target or now.weekday() >= 5:
        # Move to next weekday
        days_ahead = 1
        while True:
            candidate = now + __import__('datetime').timedelta(days=days_ahead)
            if candidate.weekday() < 5:
                target = candidate.replace(hour=9, minute=30, second=0, microsecond=0)
                break
            days_ahead += 1
    return max(0, int((target - now).total_seconds()))


# ---------------------------------------------------------------------------
# Market data (Twelve Data)
# ---------------------------------------------------------------------------
def get_price_data() -> pd.DataFrame:
    num_points = config.LONG_MA * 3
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     config.TWELVE_DATA_SYMBOL,
        "interval":   "1min",
        "outputsize": num_points,
        "apikey":     config.TWELVE_DATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("status") == "error":
        raise ValueError(f"Twelve Data error: {data.get('message', 'unknown')}")

    values = data.get("values", [])
    if not values:
        raise ValueError(f"No price data from Twelve Data for {config.TWELVE_DATA_SYMBOL}")

    rows = [{
        "time":   v["datetime"],
        "open":   float(v["open"]),
        "high":   float(v["high"]),
        "low":    float(v["low"]),
        "close":  float(v["close"]),
        "volume": float(v.get("volume", 0)),
    } for v in values]

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)   # oldest first
    return df


def compute_signal(bars: pd.DataFrame) -> str:
    bars = bars.copy()
    bars["short_ma"] = bars["close"].rolling(window=config.SHORT_MA).mean()
    bars["long_ma"]  = bars["close"].rolling(window=config.LONG_MA).mean()
    bars = bars.dropna(subset=["short_ma", "long_ma"])
    if len(bars) < 2:
        return "HOLD"

    prev, curr = bars.iloc[-2], bars.iloc[-1]
    if prev["short_ma"] <= prev["long_ma"] and curr["short_ma"] > curr["long_ma"]:
        return "BUY"
    if prev["short_ma"] >= prev["long_ma"] and curr["short_ma"] < curr["long_ma"]:
        return "SELL"
    return "HOLD"


# ---------------------------------------------------------------------------
# Position helpers
# Local tracking used because GET /positions/otc is not available on this account
# ---------------------------------------------------------------------------
def place_buy(client: IGClient, epic: str, last_price: float) -> dict | None:
    acc = client.get_preferred_account()
    balance   = float(acc["balance"]["balance"])
    available = float(acc["balance"]["available"])
    currency  = acc["currency"]
    dollars   = min(balance * (config.POSITION_SIZE_PCT / 100), available)

    if dollars < 1:
        alert(f"Not enough funds to buy {epic}. Available: {currency} {available:.2f}", level="warning")
        return None

    size = round(dollars / last_price, 2)
    if size <= 0:
        alert(f"Calculated size is 0 for {epic} at price {last_price:.2f}", level="warning")
        return None

    confirm = client.open_position("BUY", epic, size, config.IG_EXPIRY)
    if confirm.get("dealStatus") == "ACCEPTED":
        alert(
            f"BUY {size} x {epic} @ ~{last_price:.2f}\n"
            f"Spent: {currency} {dollars:.2f} ({config.POSITION_SIZE_PCT}% of {balance:,.2f})\n"
            f"Deal ID: {confirm.get('dealId', 'N/A')}"
        )
        return {"dealId": confirm["dealId"], "epic": epic, "size": size, "entry": confirm.get("level", last_price)}
    else:
        alert(f"BUY rejected for {epic}: {confirm.get('reason', 'unknown reason')}", level="warning")
        return None


def place_sell(client: IGClient, position: dict, last_price: float) -> bool:
    deal_id = position["dealId"]
    size    = position["size"]
    epic    = position["epic"]

    confirm = client.close_position(deal_id, "SELL", epic, size)
    if confirm.get("dealStatus") == "ACCEPTED":
        entry = position.get("entry", last_price)
        pnl   = (last_price - entry) * size
        alert(
            f"SELL {size} x {epic} @ ~{last_price:.2f}\n"
            f"Entry: {entry:.2f} | Est. P&L: {pnl:+.2f}\n"
            f"Deal ID: {confirm.get('dealId', 'N/A')}"
        )
        return True
    else:
        alert(f"SELL rejected for {epic}: {confirm.get('reason', 'unknown reason')}", level="warning")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_bot() -> None:
    client = startup_checks()
    epic = config.IG_EPIC

    acc = client.get_preferred_account()
    alert(
        f"Trading bot started (IG Markets)\n"
        f"Epic: {epic}\n"
        f"Strategy: MA crossover ({config.SHORT_MA}/{config.LONG_MA})\n"
        f"Position size: {config.POSITION_SIZE_PCT}% of balance\n"
        f"Account: {acc['accountName']} ({config.IG_ACCOUNT_TYPE})\n"
        f"Balance: {acc['currency']} {float(acc['balance']['balance']):,.2f}\n"
        f"Market hours only: Mon-Fri 9:30 AM - 4:00 PM ET"
    )

    open_position  = None   # tracks current open trade: {dealId, epic, size, entry}
    market_was_open    = False
    last_open_report_date  = None
    last_close_report_date = None

    while True:
        try:
            market_hours = is_us_market_open()
            today = datetime.now().date()

            # Market open/close reports
            if market_hours and not market_was_open and last_open_report_date != today:
                try:
                    generate_report("open", client)
                    last_open_report_date = today
                except Exception as e:
                    log.error(f"Open report failed: {e}")

            if not market_hours and market_was_open and last_close_report_date != today:
                # Close any open position at end of day
                if open_position:
                    bars = get_price_data()
                    last_price = float(bars["close"].iloc[-1])
                    log.info("Market closing — closing open position...")
                    if place_sell(client, open_position, last_price):
                        open_position = None
                try:
                    generate_report("close", client)
                    last_close_report_date = today
                except Exception as e:
                    log.error(f"Close report failed: {e}")

            market_was_open = market_hours

            if not market_hours:
                secs = seconds_until_market_open()
                if secs > 60:
                    log.info(f"Market closed. Next open in {secs // 3600}h {(secs % 3600) // 60}m. Sleeping...")
                    time.sleep(min(secs - 60, 300))
                else:
                    time.sleep(30)
                continue

            bars       = get_price_data()
            signal     = compute_signal(bars)
            last_price = float(bars["close"].iloc[-1])

            log.info(f"{epic} @ {last_price:.2f} | signal={signal} | position={'open' if open_position else 'none'}")

            if signal == "BUY" and open_position is None:
                open_position = place_buy(client, epic, last_price)
            elif signal == "SELL" and open_position is not None:
                if place_sell(client, open_position, last_price):
                    open_position = None

        except KeyboardInterrupt:
            alert("Bot stopped manually")
            break
        except Exception as e:
            tb = traceback.format_exc()
            log.error(f"Main loop error:\n{tb}")
            if "401" in str(e) or "403" in str(e):
                log.info("Session may have expired — re-logging in...")
                try:
                    client.login()
                    log.info("Re-login successful")
                except Exception as re:
                    log.error(f"Re-login failed: {re}")
            alert(f"Error in main loop: {e}", level="error")

        time.sleep(config.CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_bot()
