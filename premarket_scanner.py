"""
=============================================================================
  Pre-Market Scanner
=============================================================================
  Runs every 5 minutes during US pre-market hours (4:00 AM - 9:30 AM ET).
  Finds stocks where 70%+ of pre-market 1-minute candles are bullish (green)
  AND price is up from yesterday's close.
  Sends results to Telegram.
=============================================================================
"""

import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytz
import yfinance as yf

import config
from utils import log, send_telegram, validate_config, test_telegram_connection
from scanner import _fetch_all_us
from claude_analyst import analyse_premarket_signal
from reports import generate_report

ET = pytz.timezone("America/New_York")
PREMARKET_START = (4, 0)   # 4:00 AM ET
PREMARKET_END   = (9, 30)  # 9:30 AM ET
SCAN_INTERVAL   = 900       # 15 minutes
TREND_THRESHOLD = 60.0      # % of green candles required
MIN_CANDLES     = 10        # minimum candles needed to evaluate

# Pre-market briefing report: fired once per morning at/after this ET time,
# shortly before the open. Uses that scan's signals as the "top movers".
BRIEFING_TIME   = (9, 0)   # 9:00 AM ET
BRIEFING_MOVERS = 10       # number of top signals to include in the briefing


def is_premarket() -> bool:
    now_et = datetime.now(ET)
    t = (now_et.hour, now_et.minute)
    return PREMARKET_START <= t < PREMARKET_END


def minutes_to_premarket() -> int:
    now_et = datetime.now(ET)
    start  = now_et.replace(hour=PREMARKET_START[0], minute=PREMARKET_START[1], second=0, microsecond=0)
    if now_et >= start:
        return 0
    return int((start - now_et).total_seconds() / 60)


def get_premarket_data(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.download(
            symbol,
            period="2d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Keep only today's pre-market candles (4:00–9:30 AM ET)
        df.index = df.index.tz_convert(ET)
        today = datetime.now(ET).date()
        df = df[df.index.date == today]
        df = df.between_time("04:00", "09:29")
        return df if not df.empty else None
    except Exception:
        return None


def get_prev_close(symbol: str) -> float | None:
    try:
        df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return float(df["Close"].iloc[-2])
    except Exception:
        return None


def analyze_premarket(symbol: str) -> dict | None:
    try:
        df = get_premarket_data(symbol)
        if df is None or len(df) < MIN_CANDLES:
            return None

        green_candles = (df["Close"] > df["Open"]).sum()
        total_candles = len(df)
        pct_green     = (green_candles / total_candles) * 100

        if pct_green < TREND_THRESHOLD:
            return None

        prev_close  = get_prev_close(symbol)
        last_price  = float(df["Close"].iloc[-1])
        if prev_close is None or prev_close <= 0:
            return None

        pct_change = (last_price / prev_close - 1) * 100
        if pct_change <= 0:
            return None

        return {
            "symbol":       symbol,
            "last_price":   last_price,
            "prev_close":   prev_close,
            "pct_change":   pct_change,
            "pct_green":    pct_green,
            "total_candles": total_candles,
            "volume":       int(df["Volume"].sum()),
        }
    except Exception as e:
        log.debug(f"Pre-market analysis failed for {symbol}: {e}")
        return None


def scan_premarket() -> tuple[list[dict], dict]:
    tickers = _fetch_all_us()
    log.info(f"Pre-market scan: {len(tickers)} stocks...")
    signals = []
    premarket_data = {}   # symbol -> DataFrame (for Claude analysis)

    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            log.info(f"  Progress: {i}/{len(tickers)} | signals: {len(signals)}")
        df = get_premarket_data(ticker)
        if df is None or len(df) < MIN_CANDLES:
            continue
        premarket_data[ticker] = df
        result = analyze_premarket(ticker)
        if result:
            signals.append(result)
            log.info(
                f"  Signal: {ticker} +{result['pct_change']:.2f}% | "
                f"{result['pct_green']:.0f}% green candles"
            )

    signals.sort(key=lambda x: (x["pct_green"], x["pct_change"]), reverse=True)
    return signals, premarket_data


def send_results(signals: list[dict], premarket_data: dict) -> None:
    now_et = datetime.now(ET).strftime("%I:%M %p ET")

    if not signals:
        send_telegram(
            f"🌅 <b>Pre-Market Scan</b> — {now_et}\n"
            f"No stocks found with {TREND_THRESHOLD:.0f}%+ bullish pre-market trend."
        )
        return

    msg = (
        f"🌅 <b>Pre-Market Scan</b> — {now_et}\n"
        f"Found <b>{len(signals)}</b> stocks with {TREND_THRESHOLD:.0f}%+ bullish candles\n\n"
    )

    for s in signals[:15]:
        # Run Claude analysis on each signal
        df = premarket_data.get(s["symbol"])
        if df is not None:
            analysis = analyse_premarket_signal(s, df)
        else:
            analysis = {"verdict": "BUY", "reason": "No candle data for AI review"}

        verdict_emoji = "🟢" if analysis["verdict"] == "BUY" else "🔴"
        msg += (
            f"📈 <b>{s['symbol']}</b> {verdict_emoji} <b>{analysis['verdict']}</b>\n"
            f"   Price: ${s['last_price']:.2f} (+{s['pct_change']:.2f}% vs prev close)\n"
            f"   Bullish candles: {s['pct_green']:.0f}% ({s['total_candles']} candles)\n"
            f"   Pre-mkt volume: {s['volume']:,}\n"
            f"   🤖 AI: {analysis['reason']}\n"
            f"   <a href='https://finance.yahoo.com/quote/{s['symbol']}'>📊 Chart</a>\n\n"
        )

    if len(signals) > 15:
        msg += f"<i>...and {len(signals) - 15} more stocks</i>\n"

    send_telegram(msg)


def send_briefing(signals: list[dict]) -> None:
    """Send the pre-market opening briefing report (index futures + account + top movers)."""
    movers = signals[:BRIEFING_MOVERS]
    log.info(f"Sending pre-market briefing report ({len(movers)} movers)...")
    generate_report("premarket", movers=movers)


def run_premarket_scanner() -> None:
    log.info("Pre-market scanner starting...")
    validate_config()
    if not test_telegram_connection():
        raise SystemExit(1)

    brief_h, brief_m = BRIEFING_TIME
    send_telegram(
        f"🌅 <b>Pre-Market Scanner started</b>\n"
        f"Schedule: every 15 min from 4:00 AM to 9:30 AM ET\n"
        f"Criteria: 60%+ bullish (green) pre-market candles + positive vs prev close\n"
        f"Universe: ALL US stocks\n"
        f"Briefing report: once daily at ~{brief_h:02d}:{brief_m:02d} ET"
    )

    last_scan_minute   = None
    last_briefing_date = None

    while True:
        try:
            now_et = datetime.now(ET)

            if not is_premarket():
                mins = minutes_to_premarket()
                if mins > 0:
                    log.info(f"Pre-market starts in {mins} min. Sleeping...")
                    # Sleep in 1-min chunks so we wake up close to 4 AM
                    time.sleep(min(60, mins * 60))
                else:
                    # Market is open or after hours — sleep 10 min and recheck
                    log.info("Outside pre-market window. Sleeping 10 min...")
                    time.sleep(600)
                continue

            # Only scan once per 5-minute window to avoid duplicate alerts
            current_window = now_et.minute // 15
            if current_window == last_scan_minute:
                time.sleep(30)
                continue

            last_scan_minute = current_window
            log.info(f"Running pre-market scan at {now_et.strftime('%I:%M %p ET')}...")
            signals, premarket_data = scan_premarket()
            send_results(signals, premarket_data)
            log.info(f"Scan complete. {len(signals)} signals sent to Telegram.")

            # Fire the pre-market briefing once per morning, at/after BRIEFING_TIME,
            # using this scan's freshest signals as the top movers.
            today = now_et.date()
            if (now_et.hour, now_et.minute) >= BRIEFING_TIME and last_briefing_date != today:
                try:
                    send_briefing(signals)
                    last_briefing_date = today
                except Exception as e:
                    log.error(f"Pre-market briefing failed: {e}")
                    send_telegram(f"⚠️ Pre-market briefing failed: {e}")

            time.sleep(30)

        except KeyboardInterrupt:
            send_telegram("🛑 <b>Pre-market scanner stopped</b>")
            break
        except Exception as e:
            log.error(f"Pre-market scanner error: {e}")
            send_telegram(f"⚠️ Pre-market scanner error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_premarket_scanner()
