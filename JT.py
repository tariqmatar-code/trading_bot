import io
import os
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt

from dotenv import load_dotenv

# ================== LOAD ENV ==================
load_dotenv()

IG_API_KEY     = os.getenv("IG_API_KEY")
IG_IDENTIFIER  = os.getenv("IG_IDENTIFIER")
IG_PASSWORD    = os.getenv("IG_PASSWORD")
IG_ACCOUNT_ID  = os.getenv("IG_ACCOUNT_ID", "")
IG_DEMO        = os.getenv("IG_ACCOUNT_TYPE", "DEMO").upper() == "DEMO"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
DEFAULT_SIZE   = float(os.getenv("DEFAULT_SIZE", "1"))

INITIAL_BALANCE = 10000

# ================== LOGGING ==================
logging.basicConfig(
    filename="jt_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ================== TELEGRAM ==================
def tg_send(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured:", msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("Telegram error:", e)

def tg_report(summary: str):
    tg_send("📊 REPORT\n" + summary)

# ================== IG CLIENT ==================
class IGClient:
    def __init__(self, api_key, identifier, password, acc_id, demo=True):
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.acc_id = acc_id
        self.demo = demo
        self.session = requests.Session()
        self.base = "https://demo-api.ig.com/gateway/deal" if demo else "https://api.ig.com/gateway/deal"
        self.auth()

    def auth(self):
        url = f"{self.base}/session"
        headers = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        data = {
            "identifier": self.identifier,
            "password": self.password
        }
        r = self.session.post(url, headers=headers, data=json.dumps(data))
        if r.status_code >= 400:
            raise Exception(f"IG auth failed: {r.text}")
        self.session.headers.update({
            "X-IG-API-KEY": self.api_key,
            "CST": r.headers["CST"],
            "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]
        })

    def _request(self, method, url, **kwargs):
        r = self.session.request(method, url, **kwargs)
        if r.status_code == 403:
            try:
                err = r.json().get("errorCode", "")
            except Exception:
                err = ""
            if "allowance" in err:
                r.raise_for_status()
            logging.info("IG session expired — re-authenticating")
            self.auth()
            r = self.session.request(method, url, **kwargs)
        r.raise_for_status()
        return r

    def prices(self, epic, resolution="H1", num=200):
        url = f"{self.base}/prices/{epic}/{resolution}/{num}"
        return self._request("GET", url).json()

    def search_markets(self, search_term):
        url = f"{self.base}/markets?searchTerm={search_term}"
        return self._request("GET", url).json()

    def place_order(self, epic, direction, size):
        url = f"{self.base}/positions/otc"
        data = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "currencyCode": "USD",
            "forceOpen": True
        }
        return self._request("POST", url, json=data).json()

    def close_position(self, deal_id, direction, size):
        url = f"{self.base}/positions/otc"
        headers = {"_method": "DELETE"}
        data = {
            "dealId": deal_id,
            "direction": direction,
            "size": size,
            "orderType": "MARKET"
        }
        return self._request("POST", url, json=data, headers=headers).json()

# ================== IG HELPERS ==================
def ig_prices_to_df(data):
    prices = data.get("prices", [])
    rows = []
    for p in prices:
        rows.append([
            p["snapshotTime"],
            float(p["openPrice"]["bid"]),
            float(p["highPrice"]["bid"]),
            float(p["lowPrice"]["bid"]),
            float(p["closePrice"]["bid"]),
            p.get("lastTradedVolume", 0)
        ])
    df = pd.DataFrame(rows, columns=["timestamp","Open","High","Low","Close","Volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.set_index("timestamp", inplace=True)
    return df

def get_epic(ig: IGClient, ticker: str):
    try:
        data = ig.search_markets(ticker)
        markets = data.get("markets", [])
        if not markets:
            return None
        return markets[0]["epic"]
    except Exception as e:
        logging.error(f"EPIC lookup failed for {ticker}: {e}")
        return None

# ================== TICKER UNIVERSE ==================
US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "XOM", "LLY", "JNJ", "WMT", "MA", "PG", "AVGO",
    "HD", "CVX", "MRK", "ABBV", "COST", "PEP", "KO", "ADBE", "NFLX",
    "CRM", "AMD", "INTC"
]

def load_us_stock_universe():
    return US_UNIVERSE

EPIC_CACHE_FILE = "epic_cache.json"

def build_epic_universe(ig: IGClient):
    if os.path.exists(EPIC_CACHE_FILE):
        with open(EPIC_CACHE_FILE) as f:
            mapping = json.load(f)
        logging.info(f"Loaded {len(mapping)} EPICs from cache")
        tg_send(f"Loaded {len(mapping)} EPICs from cache")
        return [tuple(x) for x in mapping]

    tickers = load_us_stock_universe()
    mapping = []
    for i, t in enumerate(tickers):
        epic = get_epic(ig, t)
        if epic:
            mapping.append((t, epic))
        if i % 50 == 0:
            logging.info(f"EPIC lookup progress: {i}/{len(tickers)}")
        time.sleep(1.0)

    with open(EPIC_CACHE_FILE, "w") as f:
        json.dump(mapping, f)
    logging.info(f"Built and cached {len(mapping)} EPICs")
    return mapping

# ================== REAL-TIME PRICE (YAHOO) ==================
def get_realtime_price(ticker: str):
    try:
        data = yf.Ticker(ticker).history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as e:
        logging.error(f"Yahoo price error for {ticker}: {e}")
        return None

# ================== STRATEGY: ICHIMOKU + FIBO ==================
def ichimoku(df):
    high = df['High']
    low = df['Low']
    close = df['Close']

    df['tenkan'] = (high.rolling(9).max() + low.rolling(9).min()) / 2
    df['kijun'] = (high.rolling(26).max() + low.rolling(26).min()) / 2
    df['senkou_a'] = ((df['tenkan'] + df['kijun']) / 2).shift(26)
    df['senkou_b'] = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    df['chikou'] = close.shift(-26)
    return df

def find_chikou_breakout(df):
    for i in range(60, len(df) - 26):
        chikou = df['chikou'].iloc[i]
        cloud_top = max(df['senkou_a'].iloc[i], df['senkou_b'].iloc[i])
        cloud_bottom = min(df['senkou_a'].iloc[i], df['senkou_b'].iloc[i])
        prev_chikou = df['chikou'].iloc[i - 1]
        if prev_chikou < cloud_bottom and chikou > cloud_top:
            return i
    return None

def find_all_chikou_breakouts(df):
    breakouts = []
    for i in range(60, len(df) - 26):
        chikou = float(df['chikou'].iloc[i])
        cloud_top = max(float(df['senkou_a'].iloc[i]), float(df['senkou_b'].iloc[i]))
        prev_chikou = float(df['chikou'].iloc[i - 1])
        if prev_chikou <= cloud_top and chikou > cloud_top:
            breakouts.append(i)
    return breakouts

def build_fibonacci(df, idx):
    fib_0 = df['High'].iloc[max(0, idx-20): idx+1].max()  # swing high over 20 bars
    window = df.iloc[max(0, idx-20): idx+1]
    fib_100 = window['Low'].min()  # swing low over 20 bars
    fib_382 = fib_0 - 0.382 * (fib_0 - fib_100)
    fib_618 = fib_0 - 0.618 * (fib_0 - fib_100)
    fib_900 = fib_0 - 0.900 * (fib_0 - fib_100)
    return fib_0, fib_382, fib_618, fib_900

def trend_ok(df, i):
    price = float(df['Close'].iloc[i])
    cloud_top = max(float(df['senkou_a'].iloc[i]), float(df['senkou_b'].iloc[i]))
    return price > cloud_top

# ================== BACKTEST ==================
def backtest(df, initial_balance=INITIAL_BALANCE, plot=True):
    df = ichimoku(df)
    df.dropna(inplace=True)

    breakouts = find_all_chikou_breakouts(df)
    if not breakouts:
        return initial_balance, [], "No Chikou breakout found."

    breakout_set = set(breakouts)
    balance = initial_balance
    equity_curve = []
    trades = []
    position = None
    entry_price = None
    fib_382 = fib_618 = fib_900 = tp = None
    last_breakout_bar = -50

    for i in range(breakouts[0], len(df)):
        price = float(df['Close'].iloc[i])

        # New breakout: update Fibonacci levels if no open position
        if i in breakout_set and position is None and i > last_breakout_bar + 5:
            _, fib_382, fib_618, fib_900 = build_fibonacci(df, i)
            tp = fib_0 = float(df['High'].iloc[i])
            last_breakout_bar = i

        if fib_382 is None or not trend_ok(df, i):
            equity_curve.append(balance)
            continue

        if position is None:
            if fib_618 < price <= fib_382:
                position = "LONG_38"
                entry_price = price
                tp = fib_382 * 1.03
            elif fib_900 < price <= fib_618:
                position = "LONG_61"
                entry_price = price
                tp = fib_382 * 1.03

        if position:
            if price >= tp:
                pnl = tp - entry_price
                balance += pnl
                trades.append(pnl)
                position = None
            elif price <= fib_900:
                pnl = fib_900 - entry_price
                balance += pnl
                trades.append(pnl)
                position = None

        equity_curve.append(balance)

    eq = pd.Series(equity_curve, index=df.index[-len(equity_curve):])
    if len(eq) == 0:
        return balance, trades, "No trades."

    dd = (eq / eq.cummax() - 1).min()

    if trades:
        win_rate = sum(t > 0 for t in trades) / len(trades) * 100
        profit_factor = (sum(t for t in trades if t > 0) /
                         abs(sum(t for t in trades if t < 0))) if any(t < 0 for t in trades) else float('inf')
    else:
        win_rate = 0
        profit_factor = 0

    summary = f"""Backtest Summary:
Initial: {initial_balance}
Final:   {balance:.2f}
Trades:  {len(trades)}
WinRate: {win_rate:.2f}%
PF:      {profit_factor:.2f}
MaxDD:   {dd*100:.2f}%
"""

    if plot:
        plt.figure(figsize=(10,5))
        plt.plot(eq, label="Equity")
        plt.title("Equity Curve")
        plt.legend()
        plt.show()

        plt.figure(figsize=(10,3))
        plt.plot(eq / eq.cummax() - 1, label="Drawdown")
        plt.title("Drawdown")
        plt.legend()
        plt.show()

    return balance, trades, summary

def backtest_single_ticker(ticker="CLSK"):
    df = yf.download(ticker, period="3y", interval="1d")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        print("No data for", ticker)
        return
    final_balance, trades, summary = backtest(df)
    print(summary)
    tg_report(f"Backtest {ticker}\n{summary}")

# ================== SCANNER FOR MANY EPICS (WITH YAHOO PRICE) ==================
def scan_all_markets(ig: IGClient, mapping):
    """
    mapping: list of (ticker, epic)
    """
    signals = []
    for ticker, epic in mapping:
        try:
            data = ig.prices(epic, "H1", 200)
            df = ig_prices_to_df(data)
            if df.empty:
                continue
            df = ichimoku(df)
            df.dropna(inplace=True)

            idx = find_chikou_breakout(df)
            if idx is None:
                continue

            fib_0, fib_382, fib_618, fib_900 = build_fibonacci(df, idx)
            tp = fib_382 * 1.03

            # Use Yahoo for latest real-time price
            rt_price = get_realtime_price(ticker)
            if rt_price is None:
                price = df['Close'].iloc[-1]
            else:
                price = rt_price

            if trend_ok(df, len(df)-1):
                if price <= fib_382 and price > fib_618:
                    signals.append((epic, ticker, "LONG_38", price, tp, fib_900))
                elif price <= fib_618 and price > fib_900:
                    signals.append((epic, ticker, "LONG_61", price, tp, fib_900))
        except Exception as e:
            logging.error(f"Error scanning {epic}: {e}")
    return signals

# ================== LIVE BOT (YAHOO PRICE FOR ENTRY & EXIT) ==================
def live_trading_all_us_stocks():
    ig = IGClient(IG_API_KEY, IG_IDENTIFIER, IG_PASSWORD, IG_ACCOUNT_ID, demo=IG_DEMO)
    tg_send("🤖 IG Bot Started (S&P500 + NASDAQ100)")

    mapping = build_epic_universe(ig)  # list of (ticker, epic)
    tg_send(f"Tracking {len(mapping)} EPICs")

    # epic -> (deal_id, entry_price, tp, sl, type, ticker)
    open_positions = {}

    while True:
        try:
            signals = scan_all_markets(ig, mapping)

            # Entries
            for epic, ticker, sig_type, price, tp, sl in signals:
                if epic in open_positions:
                    continue
                size = DEFAULT_SIZE
                res = ig.place_order(epic, "BUY", size)
                deal_ref = res.get("dealReference", "N/A")
                open_positions[epic] = (deal_ref, price, tp, sl, sig_type, ticker)
                msg = f"🟢 {sig_type} {epic} ({ticker})\nEntry: {price}\nTP: {tp}\nSL: {sl}"
                tg_send(msg)
                logging.info(msg)

            # Exits (using Yahoo for latest price)
            for epic in list(open_positions.keys()):
                deal_id, entry_price, tp, sl, sig_type, ticker = open_positions[epic]

                last_price = get_realtime_price(ticker)
                if last_price is None:
                    # fallback to IG last close
                    data = ig.prices(epic, "H1", 2)
                    df = ig_prices_to_df(data)
                    if df.empty:
                        continue
                    last_price = df['Close'].iloc[-1]

                if last_price >= tp:
                    ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
                    pnl = tp - entry_price
                    msg = f"✅ TP {epic} ({ticker}) {sig_type} PnL={pnl:.2f}"
                    tg_send(msg)
                    logging.info(msg)
                    del open_positions[epic]

                elif last_price <= sl:
                    ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
                    pnl = sl - entry_price
                    msg = f"❌ SL {epic} ({ticker}) {sig_type} PnL={pnl:.2f}"
                    tg_send(msg)
                    logging.info(msg)
                    del open_positions[epic]

            time.sleep(60)

        except Exception as e:
            logging.error(f"Live bot error: {e}")
            tg_send(f"⚠️ Bot error: {e}")
            time.sleep(60)

# ================== MAIN ==================
if __name__ == "__main__":
    # 1) Optional: quick backtest
    # backtest_single_ticker("TSLA")
    # backtest_single_ticker("APP")

    # 2) Start live trading on all US stocks (S&P500 + NASDAQ100)
    live_trading_all_us_stocks()
