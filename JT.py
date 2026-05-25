import io
import os
import re
import json
import time
import logging
import threading
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt

from dotenv import load_dotenv

try:
    import anthropic as _anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

# ================== LOAD ENV ==================
load_dotenv()

IG_API_KEY     = os.getenv("IG_API_KEY")
IG_IDENTIFIER  = os.getenv("IG_IDENTIFIER")
IG_PASSWORD    = os.getenv("IG_PASSWORD")
IG_ACCOUNT_ID  = os.getenv("IG_ACCOUNT_ID", "")
IG_DEMO        = os.getenv("IG_ACCOUNT_TYPE", "DEMO").upper() == "DEMO"

# Broker selector — IG (default, legacy) or ALPACA
BROKER            = os.getenv("BROKER", "IG").upper()
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "TRUE").upper() == "TRUE"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")

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

def tg_send_document(file_path: str, caption: str = ""):
    """Upload a file to Telegram via sendDocument."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (document):", file_path)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        print("Telegram document error:", e)
        tg_send(f"⚠️ Failed to send file: {e}")

MENU = (
    "📋 Menu — reply with a number:\n"
    "1 - Account report\n"
    "2 - Open positions\n"
    "3 - Trading stats\n"
    "4 - Bot status\n"
    "5 - Buy stock\n"
    "6 - Sell / Close position\n"
    "7 - Stop bot\n"
    "8 - Start bot\n"
    "9 - Top losers + gainers + most active (Claude AI)\n"
    "10 - Check orders in place (IG)\n"
    "11 - Crypto scan (Claude AI)\n"
    "12 - Pre-market scan\n"
    "13 - Backtest a ticker\n"
    "14 - Recent trade log\n"
    "15 - Cancel a pending IG order\n"
    "16 - Set position size (DEFAULT_SIZE)\n"
    "17 - Force-close ALL open positions\n"
    "18 - Auto-buy history\n"
    "19 - Live price check\n"
    "20 - Single-ticker Claude analysis\n"
    "21 - Refresh EPIC cache\n"
    "22 - Modify SL/TP of an open position\n"
    "23 - Sector scan (S&P sector ETFs)\n"
    "24 - News for a ticker\n"
    "25 - Toggle auto-buy on/off\n"
    "26 - Export trade log to CSV\n"
    "27 - Risk dashboard\n"
    "28 - Watch list / price alerts\n"
    "0 - Show this menu\n"
    "\n💡 Quick: type  buy  or  sell  to start guided order\n"
    "   Or direct: BUY AAPL 1 / SELL TSLA 2"
)

# Shared state for Telegram listener
_bot_state = {
    "ig": None,
    "open_positions": {},
    "trade_log": [],
    "auto_buy_log": [],
    "start_time": time.time(),
    "last_scan": None,
    "running": True,
    "awaiting_order": False,
    "awaiting_confirm": None,   # pending order dict waiting for y/n
    "awaiting_close": None,     # list of open positions waiting for number
    "awaiting_trade": None,          # guided buy/sell: {step, direction, ticker, size}
    "awaiting_close_confirm": None,  # pending close waiting for y/n
    "awaiting_backtest": False,      # waiting for ticker symbol
    "awaiting_cancel_order": None,   # list of working orders waiting for number
    "awaiting_set_size": False,      # waiting for new DEFAULT_SIZE value
    "awaiting_price_check": False,
    "awaiting_claude_ticker": False,
    "awaiting_news_ticker": False,
    "awaiting_modify_sltp": None,    # {step, pos_list, epic, sl, tp, ticker}
    "awaiting_watch_input": False,
    "auto_buy_enabled": True,
    "watch_list": [],                # [{ticker, level, direction}]
}

def _execute_order(ig, direction: str, ticker: str, size: float):
    """Place an order and send Telegram confirmation with fill price."""
    try:
        epic = get_epic(ig, ticker)
        if not epic:
            tg_send(f"⚠️ Could not find EPIC for {ticker}.")
            return
        buy_price = get_realtime_price(ticker)
        res = ig.place_order(epic, direction, size)
        ref = res.get("dealReference", "N/A")
        price_str = f"@ ${buy_price:.2f}" if buy_price else ""
        tg_send(
            f"✅ {direction} {ticker} x{size} placed {price_str}\n"
            f"Ref: {ref}"
        )
    except Exception as e:
        tg_send(f"❌ Order failed: {e}")


def tg_handle_command(text: str):
    global DEFAULT_SIZE
    cmd = text.strip()
    ig  = _bot_state.get("ig")

    # ── Confirm pending buy/sell (y / n) ──────────────────
    if _bot_state.get("awaiting_confirm"):
        order = _bot_state["awaiting_confirm"]
        _bot_state["awaiting_confirm"] = None
        if cmd.lower() in ("y", "yes"):
            if not ig:
                tg_send("⚠️ Bot not connected.")
                return
            threading.Thread(
                target=_execute_order,
                args=(ig, order["direction"], order["ticker"], order["size"]),
                daemon=True
            ).start()
        else:
            tg_send("❌ Order cancelled.")
        return

    # ── Close: confirm step (y/n after seeing buy/sell prices) ──
    if _bot_state.get("awaiting_close_confirm") is not None:
        pending = _bot_state["awaiting_close_confirm"]
        _bot_state["awaiting_close_confirm"] = None
        if cmd.lower() not in ("y", "yes"):
            tg_send("❌ Close cancelled.")
            return
        epic      = pending["epic"]
        deal_id   = pending["deal_id"]
        entry     = pending["entry"]
        sl        = pending["sl"]
        sig       = pending["sig"]
        ticker    = pending["ticker"]
        sell_price = pending["sell_price"]
        if not ig:
            tg_send("⚠️ Bot not connected.")
            return
        try:
            ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
            pnl = sell_price - entry
            _bot_state["trade_log"].append(pnl)
            _bot_state["open_positions"].pop(epic, None)
            tg_send(
                f"✅ Position closed — {ticker} ({sig})\n"
                f"📥 Bought @ ${entry:.2f}\n"
                f"📤 Sold   @ ${sell_price:.2f}\n"
                f"💰 P&L:     {pnl:+.2f}"
            )
        except Exception as e:
            tg_send(f"❌ Close failed: {e}")
        return

    # ── Close position by number ───────────────────────────
    if _bot_state.get("awaiting_close") is not None:
        pos_list = _bot_state["awaiting_close"]
        _bot_state["awaiting_close"] = None
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        try:
            idx = int(cmd) - 1
            if idx < 0 or idx >= len(pos_list):
                raise ValueError
        except ValueError:
            tg_send("Invalid selection. Cancelled.")
            return
        epic, (deal_id, entry, tp, sl, sig, ticker) = pos_list[idx]
        sell_price = get_realtime_price(ticker) or sl
        pnl        = sell_price - entry
        # Store pending close and ask for confirmation
        _bot_state["awaiting_close_confirm"] = {
            "epic": epic, "deal_id": deal_id, "entry": entry,
            "sl": sl, "sig": sig, "ticker": ticker, "sell_price": sell_price,
        }
        tg_send(
            f"⚠️ Confirm close {ticker} ({sig})\n"
            f"📥 Bought @ ${entry:.2f}\n"
            f"📤 Sell   @ ${sell_price:.2f}\n"
            f"💰 P&L:     {pnl:+.2f}\n\n"
            f"Reply y to confirm or n to cancel"
        )
        return

    # ── Guided trade flow (multi-step) ────────────────────
    if _bot_state.get("awaiting_trade") is not None:
        trade = _bot_state["awaiting_trade"]
        if cmd.lower() in ("n", "no", "cancel"):
            _bot_state["awaiting_trade"] = None
            tg_send("❌ Order cancelled.")
            return
        if trade["step"] == "ticker":
            trade["ticker"] = cmd.upper()
            trade["step"]   = "size"
            _bot_state["awaiting_trade"] = trade
            tg_send(f"How many shares of {trade['ticker']}?\n(type a number, e.g. 1)")
            return
        if trade["step"] == "size":
            try:
                trade["size"] = float(cmd.replace(",", "."))
            except ValueError:
                tg_send("Invalid number. How many shares? (e.g. 1)")
                return
            trade["step"] = "confirm"
            _bot_state["awaiting_trade"] = trade
            price = get_realtime_price(trade["ticker"])
            price_str = f"@ ${price:.2f}" if price else ""
            tg_send(
                f"⚠️ Confirm order:\n"
                f"{trade['direction']} {trade['ticker']} x{trade['size']} {price_str}\n"
                f"Reply y to confirm or n to cancel"
            )
            return
        if trade["step"] == "confirm":
            _bot_state["awaiting_trade"] = None
            if cmd.lower() in ("y", "yes"):
                if not ig:
                    tg_send("⚠️ Bot not connected.")
                    return
                threading.Thread(
                    target=_execute_order,
                    args=(ig, trade["direction"], trade["ticker"], trade["size"]),
                    daemon=True
                ).start()
            else:
                tg_send("❌ Order cancelled.")
            return

    # ── Backtest: ticker prompt ───────────────────────────
    if _bot_state.get("awaiting_backtest"):
        _bot_state["awaiting_backtest"] = False
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        ticker = cmd.upper().strip()
        threading.Thread(target=_run_backtest, args=(ticker,), daemon=True).start()
        return

    # ── Cancel order: select from pending ─────────────────
    if _bot_state.get("awaiting_cancel_order") is not None:
        orders = _bot_state["awaiting_cancel_order"]
        _bot_state["awaiting_cancel_order"] = None
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        try:
            idx = int(cmd) - 1
            if idx < 0 or idx >= len(orders):
                raise ValueError
        except ValueError:
            tg_send("Invalid selection. Cancelled.")
            return
        if not ig:
            tg_send("⚠️ Bot not connected.")
            return
        deal_id = orders[idx].get("workingOrderData", {}).get("dealId")
        if not deal_id:
            tg_send("⚠️ Missing deal ID.")
            return
        try:
            ig.delete_working_order(deal_id)
            tg_send(f"✅ Cancelled order {deal_id}")
        except Exception as e:
            tg_send(f"❌ Cancel failed: {e}")
        return

    # ── Set DEFAULT_SIZE ──────────────────────────────────
    if _bot_state.get("awaiting_set_size"):
        _bot_state["awaiting_set_size"] = False
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        try:
            new_size = float(cmd.replace(",", "."))
            if new_size <= 0:
                raise ValueError
        except ValueError:
            tg_send("Invalid size. Cancelled.")
            return
        DEFAULT_SIZE = new_size
        tg_send(f"✅ DEFAULT_SIZE set to {DEFAULT_SIZE}")
        return

    # ── Live price check (cmd 19) ──────────────────────────
    if _bot_state.get("awaiting_price_check"):
        _bot_state["awaiting_price_check"] = False
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        threading.Thread(target=_check_price, args=(cmd.upper().strip(),), daemon=True).start()
        return

    # ── Single ticker Claude analysis (cmd 20) ─────────────
    if _bot_state.get("awaiting_claude_ticker"):
        _bot_state["awaiting_claude_ticker"] = False
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        threading.Thread(target=_analyze_single_ticker, args=(cmd.upper().strip(),), daemon=True).start()
        return

    # ── News for a ticker (cmd 24) ─────────────────────────
    if _bot_state.get("awaiting_news_ticker"):
        _bot_state["awaiting_news_ticker"] = False
        if cmd.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        threading.Thread(target=_get_news, args=(cmd.upper().strip(),), daemon=True).start()
        return

    # ── Modify SL/TP multi-step (cmd 22) ───────────────────
    if _bot_state.get("awaiting_modify_sltp") is not None:
        mod = _bot_state["awaiting_modify_sltp"]
        if cmd.lower() in ("n", "no", "cancel"):
            _bot_state["awaiting_modify_sltp"] = None
            tg_send("Cancelled.")
            return
        if mod["step"] == "select":
            try:
                idx = int(cmd) - 1
                if idx < 0 or idx >= len(mod["pos_list"]):
                    raise ValueError
            except ValueError:
                tg_send("Invalid selection. Cancelled.")
                _bot_state["awaiting_modify_sltp"] = None
                return
            epic, (deal_id, entry, tp, sl, sig, ticker) = mod["pos_list"][idx]
            mod["epic"]   = epic
            mod["ticker"] = ticker
            mod["entry"]  = entry
            mod["sl"]     = sl
            mod["tp"]     = tp
            mod["step"]   = "sl"
            _bot_state["awaiting_modify_sltp"] = mod
            tg_send(
                f"Selected {ticker} (entry ${entry:.2f}, SL ${sl:.2f}, TP ${tp:.2f})\n"
                f"New SL? Type a number, or 'skip' to keep ${sl:.2f}, or n to cancel"
            )
            return
        if mod["step"] == "sl":
            if cmd.lower() != "skip":
                try:
                    mod["sl"] = float(cmd.replace(",", "."))
                except ValueError:
                    tg_send("Invalid number. Cancelled.")
                    _bot_state["awaiting_modify_sltp"] = None
                    return
            mod["step"] = "tp"
            _bot_state["awaiting_modify_sltp"] = mod
            tg_send(f"New TP? Type a number, or 'skip' to keep ${mod['tp']:.2f}, or n to cancel")
            return
        if mod["step"] == "tp":
            if cmd.lower() != "skip":
                try:
                    mod["tp"] = float(cmd.replace(",", "."))
                except ValueError:
                    tg_send("Invalid number. Cancelled.")
                    _bot_state["awaiting_modify_sltp"] = None
                    return
            # Apply update
            pos = _bot_state.get("open_positions", {})
            if mod["epic"] in pos:
                deal_id, _entry, _tp, _sl, sig, ticker = pos[mod["epic"]]
                pos[mod["epic"]] = (deal_id, _entry, mod["tp"], mod["sl"], sig, ticker)
                tg_send(
                    f"✅ Updated {mod['ticker']}\n"
                    f"  SL: ${mod['sl']:.2f}\n"
                    f"  TP: ${mod['tp']:.2f}"
                )
            else:
                tg_send(f"⚠️ Position for {mod['ticker']} no longer exists.")
            _bot_state["awaiting_modify_sltp"] = None
            return

    # ── Watch list input (cmd 28) ──────────────────────────
    if _bot_state.get("awaiting_watch_input"):
        _bot_state["awaiting_watch_input"] = False
        line = cmd.strip()
        if line.lower() in ("n", "no", "cancel"):
            tg_send("Cancelled.")
            return
        if line.lower() == "clear":
            _bot_state["watch_list"] = []
            tg_send("✅ Watch list cleared.")
            return
        parts = line.split()
        if len(parts) < 3 or parts[0].lower() != "add":
            tg_send("Format: add TICKER LEVEL above|below   (or clear, or n)")
            return
        try:
            ticker = parts[1].upper()
            level  = float(parts[2])
            direction = parts[3].lower() if len(parts) >= 4 else "above"
            if direction not in ("above", "below"):
                raise ValueError
        except (ValueError, IndexError):
            tg_send("Invalid format. Example: add AAPL 200 above")
            return
        _bot_state.setdefault("watch_list", []).append({
            "ticker": ticker, "level": level, "direction": direction,
        })
        tg_send(f"✅ Watching {ticker} {direction} ${level:.2f}")
        return

    # ── Direct trade: BUY TSLA / BUY TSLA 2 / SELL MSFT ──
    parts = cmd.upper().split()
    if len(parts) >= 2 and parts[0] in ("BUY", "SELL"):
        direction = parts[0]
        ticker    = parts[1]
        try:
            size = float(parts[2]) if len(parts) >= 3 else DEFAULT_SIZE
        except ValueError:
            tg_send("Invalid size. Example: BUY TSLA 1")
            return
        price = get_realtime_price(ticker)
        price_str = f"@ ${price:.2f}" if price else ""
        _bot_state["awaiting_confirm"] = {"direction": direction, "ticker": ticker, "size": size}
        tg_send(
            f"⚠️ Confirm order:\n"
            f"{direction} {ticker} x{size} {price_str}\n"
            f"Reply y to confirm or n to cancel"
        )
        return

    # ── Menu commands ──────────────────────────────────────
    if cmd == "0":
        tg_send(MENU)

    elif cmd == "1":
        if ig:
            send_account_report(ig)
        else:
            tg_send("⚠️ Bot not connected yet.")

    elif cmd == "2":
        pos = _bot_state.get("open_positions", {})
        if not pos:
            tg_send("No open positions.")
        else:
            lines = ["📂 Open Positions:"]
            for epic, (deal_id, entry, tp, sl, sig, ticker) in pos.items():
                current = get_realtime_price(ticker)
                pnl_str = f"  P&L: {current - entry:+.2f}" if current else ""
                price_str = f"  Now: ${current:.2f}" if current else ""
                lines.append(
                    f"  {ticker} ({sig})\n"
                    f"  Buy: ${entry:.2f}{price_str}{pnl_str}\n"
                    f"  TP: ${tp:.2f}  SL: ${sl:.2f}"
                )
            tg_send("\n".join(lines))

    elif cmd == "3":
        trades = _bot_state.get("trade_log", [])
        if not trades:
            tg_send("No trades yet.")
        else:
            wins      = sum(1 for t in trades if t > 0)
            losses    = sum(1 for t in trades if t <= 0)
            total_pnl = sum(trades)
            tg_send(
                f"📈 Trading Stats\n"
                f"Trades: {len(trades)}\n"
                f"Wins:   {wins} | Losses: {losses}\n"
                f"Total P&L: {total_pnl:.2f}"
            )

    elif cmd == "4":
        uptime = int(time.time() - _bot_state.get("start_time", time.time()))
        h, m   = divmod(uptime // 60, 60)
        last   = _bot_state.get("last_scan") or "not yet"
        status = "▶️ Running" if _bot_state.get("running") else "⏸ Paused"
        tg_send(
            f"🤖 Bot Status: {status}\n"
            f"Uptime: {h}h {m}m\n"
            f"Last scan: {last}\n"
            f"Open positions: {len(_bot_state.get('open_positions', {}))}"
        )

    elif cmd in ("5", "BUY", "buy"):
        _bot_state["awaiting_trade"] = {"step": "ticker", "direction": "BUY", "ticker": None, "size": None}
        tg_send("📈 BUY — Which stock?\nType the ticker symbol (e.g. AAPL, TSLA, NVDA, MSFT)\nor n to cancel")

    elif cmd in ("6", "SELL", "sell"):
        pos = _bot_state.get("open_positions", {})
        if not pos:
            tg_send("No open positions to close.")
            return
        lines = ["📂 Select position to close (reply with number):"]
        pos_list = list(pos.items())
        for i, (epic, (deal_id, entry, tp, sl, sig, ticker)) in enumerate(pos_list, 1):
            current = get_realtime_price(ticker)
            if current:
                pnl = current - entry
                lines.append(f"  {i}. {ticker}  Buy: ${entry:.2f} → Now: ${current:.2f}  P&L: {pnl:+.2f}")
            else:
                lines.append(f"  {i}. {ticker}  Buy: ${entry:.2f}")
        lines.append("\nReply n to cancel")
        _bot_state["awaiting_close"] = pos_list
        tg_send("\n".join(lines))

    elif cmd == "7":
        _bot_state["running"] = False
        tg_send("⏸ Bot paused. Auto-scanning stopped.\nSend 8 to resume.")

    elif cmd == "8":
        _bot_state["running"] = True
        tg_send("▶️ Bot resumed. Auto-scanning active.")

    elif cmd == "9":
        threading.Thread(target=analyze_top_losers, daemon=True).start()

    elif cmd == "10":
        if ig:
            threading.Thread(target=send_orders_report, args=(ig,), daemon=True).start()
        else:
            tg_send("⚠️ Bot not connected yet.")

    elif cmd == "11":
        threading.Thread(target=analyze_crypto, daemon=True).start()

    elif cmd == "12":
        threading.Thread(target=run_premarket_scan_once, daemon=True).start()

    elif cmd == "13":
        _bot_state["awaiting_backtest"] = True
        tg_send("📊 Backtest — which ticker?\nType the symbol (e.g. AAPL, TSLA) or n to cancel")

    elif cmd == "14":
        trades = _bot_state.get("trade_log", [])
        if not trades:
            tg_send("No trades yet.")
        else:
            recent = trades[-10:]
            lines = [f"📒 Recent Trade Log (last {len(recent)} of {len(trades)}):"]
            for i, pnl in enumerate(recent, 1):
                marker = "🟢" if pnl > 0 else "🔴"
                lines.append(f"  {i}. {marker} {pnl:+.2f}")
            lines.append(f"\nTotal P&L: {sum(trades):+.2f}")
            tg_send("\n".join(lines))

    elif cmd == "15":
        if not ig:
            tg_send("⚠️ Bot not connected.")
            return
        try:
            orders = ig.get_working_orders()
        except Exception as e:
            tg_send(f"⚠️ Failed to fetch working orders: {e}")
            return
        if not orders:
            tg_send("No pending orders.")
            return
        lines = ["📋 Pending orders — reply with number to cancel:"]
        for i, o in enumerate(orders, 1):
            wo = o.get("workingOrderData", {})
            md = o.get("marketData", {})
            epic = wo.get("epic", "?")
            direction = wo.get("direction", "?")
            size = wo.get("orderSize", "?")
            level = wo.get("orderLevel", "?")
            name = md.get("instrumentName", epic)
            lines.append(f"  {i}. {direction} {name} x{size} @ {level}")
        lines.append("\nReply n to cancel")
        _bot_state["awaiting_cancel_order"] = orders
        tg_send("\n".join(lines))

    elif cmd == "16":
        _bot_state["awaiting_set_size"] = True
        tg_send(f"Current DEFAULT_SIZE: {DEFAULT_SIZE}\nReply with new size (e.g. 1, 0.5, 2) or n to cancel")

    elif cmd == "17":
        if not ig:
            tg_send("⚠️ Bot not connected.")
            return
        pos = _bot_state.get("open_positions", {})
        if not pos:
            tg_send("No open positions to close.")
            return
        threading.Thread(target=_force_close_all, args=(ig,), daemon=True).start()

    elif cmd == "18":
        log_entries = _bot_state.get("auto_buy_log", [])
        if not log_entries:
            tg_send("No auto-buys yet.")
        else:
            recent = log_entries[-15:]
            lines = [f"🤖 Auto-Buy History (last {len(recent)} of {len(log_entries)}):"]
            for e in recent:
                lines.append(
                    f"  {e['time']} {e['ticker']}  "
                    f"entry ${e['entry']:.2f}  TP ${e['tp']:.2f}  SL ${e['sl']:.2f}"
                )
            tg_send("\n".join(lines))

    elif cmd == "19":
        _bot_state["awaiting_price_check"] = True
        tg_send("💲 Price check — which ticker?\nType the symbol (e.g. AAPL) or n to cancel")

    elif cmd == "20":
        _bot_state["awaiting_claude_ticker"] = True
        tg_send("🧠 Single-ticker Claude analysis — which ticker?\nType the symbol or n to cancel")

    elif cmd == "21":
        if not ig:
            tg_send("⚠️ Bot not connected.")
            return
        threading.Thread(target=_refresh_epic_cache, args=(ig,), daemon=True).start()

    elif cmd == "22":
        pos = _bot_state.get("open_positions", {})
        if not pos:
            tg_send("No open positions to modify.")
            return
        pos_list = list(pos.items())
        lines = ["✏️ Modify SL/TP — pick a position:"]
        for i, (epic, (deal_id, entry, tp, sl, sig, ticker)) in enumerate(pos_list, 1):
            lines.append(f"  {i}. {ticker}  entry ${entry:.2f}  SL ${sl:.2f}  TP ${tp:.2f}")
        lines.append("\nReply with a number, or n to cancel")
        _bot_state["awaiting_modify_sltp"] = {"step": "select", "pos_list": pos_list}
        tg_send("\n".join(lines))

    elif cmd == "23":
        threading.Thread(target=_sector_scan, daemon=True).start()

    elif cmd == "24":
        _bot_state["awaiting_news_ticker"] = True
        tg_send("📰 News — which ticker?\nType the symbol or n to cancel")

    elif cmd == "25":
        _bot_state["auto_buy_enabled"] = not _bot_state.get("auto_buy_enabled", True)
        state_str = "ON" if _bot_state["auto_buy_enabled"] else "OFF"
        tg_send(f"🤖 Auto-buy is now {state_str}")

    elif cmd == "26":
        threading.Thread(target=_export_trade_log_csv, daemon=True).start()

    elif cmd == "27":
        threading.Thread(target=_risk_dashboard, daemon=True).start()

    elif cmd == "28":
        watches = _bot_state.get("watch_list", [])
        lines = [f"🔔 Watch list ({len(watches)} active):"]
        for w in watches:
            lines.append(f"  {w['ticker']} {w['direction']} ${w['level']:.2f}")
        lines.append(
            "\nReply 'add TICKER LEVEL above|below' to add a watch,\n"
            "'clear' to remove all, or n to cancel"
        )
        _bot_state["awaiting_watch_input"] = True
        tg_send("\n".join(lines))

    else:
        tg_send(MENU)

def tg_listener():
    if not TELEGRAM_TOKEN:
        return
    offset = 0
    url_updates = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    while True:
        try:
            r = requests.get(url_updates, params={"offset": offset, "timeout": 30}, timeout=35)
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id == str(TELEGRAM_CHAT_ID) and text:
                    tg_handle_command(text)
        except Exception as e:
            logging.error(f"Telegram listener error: {e}")
            time.sleep(5)

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

    def get_positions(self):
        """Return all open positions from IG."""
        return self._request("GET", f"{self.base}/positions").json().get("positions", [])

    def get_working_orders(self):
        """Return all pending (working) orders from IG."""
        return self._request("GET", f"{self.base}/workingorders").json().get("workingOrders", [])

    def delete_working_order(self, deal_id):
        """Cancel a pending (working) IG order."""
        url = f"{self.base}/workingorders/otc/{deal_id}"
        headers = {"_method": "DELETE"}
        return self._request("POST", url, headers=headers).json()

    def get_account(self):
        """Return account info in unified shape (used by send_account_report)."""
        r = self._request("GET", f"{self.base}/accounts")
        accounts = r.json().get("accounts", [])
        acc = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        bal = acc.get("balance", {})
        return {
            "balance":   bal.get("balance", "N/A"),
            "available": bal.get("available", "N/A"),
            "deposit":   bal.get("deposit", "N/A"),
            "currency":  acc.get("currency", ""),
            "pnl":       bal.get("profitLoss", "N/A"),
        }


# ================== ALPACA CLIENT ==================
class AlpacaClient:
    """
    Drop-in replacement for IGClient. Uses alpaca-py SDK for orders/positions
    and yfinance for historical bars (per user choice).

    Returns data shaped to match IGClient's responses so the rest of the bot
    (scan_all_markets, send_orders_report, etc.) works unchanged.
    """

    def __init__(self, api_key, secret, paper=True):
        if not api_key or not secret:
            raise Exception("Alpaca API key/secret missing in .env (ALPACA_API_KEY, ALPACA_SECRET_KEY)")
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import (
            MarketOrderRequest, GetOrdersRequest, ClosePositionRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
        self._OrderSide = OrderSide
        self._TimeInForce = TimeInForce
        self._QueryOrderStatus = QueryOrderStatus
        self._MarketOrderRequest = MarketOrderRequest
        self._GetOrdersRequest = GetOrdersRequest
        self._ClosePositionRequest = ClosePositionRequest
        self.client = TradingClient(api_key, secret, paper=paper)
        self.paper = paper
        self.base = "paper" if paper else "live"

    def prices(self, epic, resolution="H1", num=200):
        """Fetch bars via yfinance, return in IG-compatible nested-dict shape."""
        interval_map = {"H1": "60m", "D1": "1d", "M15": "15m", "M5": "5m", "M1": "1m"}
        period_map   = {"H1": "60d", "D1": "2y", "M15": "30d", "M5": "15d", "M1": "7d"}
        interval = interval_map.get(resolution, "60m")
        period   = period_map.get(resolution, "60d")
        try:
            df = yf.download(epic, period=period, interval=interval, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.tail(num)
            prices = []
            for ts, row in df.iterrows():
                o = float(row["Open"]); h = float(row["High"])
                l = float(row["Low"]);  c = float(row["Close"])
                v = int(row.get("Volume", 0) or 0)
                prices.append({
                    "snapshotTime": ts.strftime("%Y/%m/%d %H:%M:%S"),
                    "openPrice":   {"bid": o},
                    "highPrice":   {"bid": h},
                    "lowPrice":    {"bid": l},
                    "closePrice":  {"bid": c},
                    "lastTradedVolume": v,
                })
            return {"prices": prices}
        except Exception as e:
            logging.error(f"Alpaca/yfinance price fetch for {epic}: {e}")
            return {"prices": []}

    def search_markets(self, search_term):
        """Alpaca trades symbols directly — return synthetic 'market' record."""
        try:
            asset = self.client.get_asset(search_term)
            return {"markets": [{
                "epic": asset.symbol,
                "instrumentName": getattr(asset, "name", None) or asset.symbol,
            }]}
        except Exception:
            return {"markets": []}

    def place_order(self, epic, direction, size):
        """epic == symbol for Alpaca. direction = 'BUY' or 'SELL'."""
        side = self._OrderSide.BUY if direction.upper() == "BUY" else self._OrderSide.SELL
        req = self._MarketOrderRequest(
            symbol=epic,
            qty=size,
            side=side,
            time_in_force=self._TimeInForce.DAY,
        )
        order = self.client.submit_order(req)
        return {"dealReference": str(order.id), "dealId": str(order.id)}

    def close_position(self, deal_id, direction, size):
        """For Alpaca, deal_id is the symbol (mapped that way in get_positions)."""
        try:
            self.client.close_position(
                deal_id,
                close_options=self._ClosePositionRequest(qty=str(size)),
            )
            return {"dealReference": deal_id}
        except Exception as e:
            raise Exception(f"Alpaca close failed: {e}")

    def get_positions(self):
        """Return positions shaped like IG's get_positions response."""
        positions = self.client.get_all_positions()
        out = []
        for p in positions:
            qty = float(p.qty)
            avg = float(p.avg_entry_price)
            upl = float(p.unrealized_pl)
            current = float(p.current_price) if getattr(p, "current_price", None) else 0
            out.append({
                "position": {
                    "dealId": p.symbol,
                    "direction": "BUY" if qty > 0 else "SELL",
                    "size": abs(qty),
                    "openLevel": avg,
                    "level": avg,
                    "upl": upl,
                },
                "market": {
                    "epic": p.symbol,
                    "instrumentName": p.symbol,
                    "bid": current,
                },
            })
        return out

    def get_working_orders(self):
        """Open (pending) Alpaca orders in IG-compatible shape."""
        req = self._GetOrdersRequest(status=self._QueryOrderStatus.OPEN)
        orders = self.client.get_orders(filter=req)
        out = []
        for o in orders:
            qty   = float(o.qty) if o.qty else 0
            lvl   = float(o.limit_price) if getattr(o, "limit_price", None) else 0
            otype = (o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type)).upper()
            side  = (o.side.value if hasattr(o.side, "value") else str(o.side)).upper()
            entry = {
                "workingOrderData": {
                    "dealId": str(o.id),
                    "epic": o.symbol,
                    "direction": side,
                    "orderSize": qty,
                    "orderLevel": lvl,
                    "orderType": otype,
                },
                "marketData": {
                    "instrumentName": o.symbol,
                    "epic": o.symbol,
                },
                "workingOrder": {
                    "dealId": str(o.id),
                    "direction": side,
                    "size": qty,
                    "orderType": otype,
                    "level": lvl,
                },
                "market": {
                    "instrumentName": o.symbol,
                    "epic": o.symbol,
                },
            }
            out.append(entry)
        return out

    def delete_working_order(self, deal_id):
        self.client.cancel_order_by_id(deal_id)
        return {"dealReference": deal_id}

    def get_account(self):
        """Return Alpaca account info shaped like IGClient.get_account()."""
        acc = self.client.get_account()
        return {
            "balance":   float(acc.equity),
            "available": float(acc.buying_power),
            "deposit":   float(acc.cash),
            "currency":  acc.currency,
            "pnl":       float(acc.equity) - float(acc.last_equity),
        }


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

def get_epic(broker, ticker: str):
    # Alpaca trades symbols directly — no EPIC concept.
    if isinstance(broker, AlpacaClient):
        return ticker
    try:
        data = broker.search_markets(ticker)
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

def build_epic_universe(broker):
    # Alpaca: skip EPIC lookup entirely — symbol IS the identifier.
    if isinstance(broker, AlpacaClient):
        mapping = [(t, t) for t in load_us_stock_universe()]
        tg_send(f"Alpaca: tracking {len(mapping)} symbols directly (no EPIC lookup)")
        return mapping

    if os.path.exists(EPIC_CACHE_FILE):
        with open(EPIC_CACHE_FILE) as f:
            mapping = json.load(f)
        logging.info(f"Loaded {len(mapping)} EPICs from cache")
        tg_send(f"Loaded {len(mapping)} EPICs from cache")
        return [tuple(x) for x in mapping]

    tickers = load_us_stock_universe()
    mapping = []
    for i, t in enumerate(tickers):
        epic = get_epic(broker, t)
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

# ================== ORDERS REPORT ==================
def send_orders_report(ig: IGClient):
    """Send all open positions + pending working orders to Telegram."""
    lines = ["📋 Orders In Place\n"]

    # ── Open positions (filled trades) ──────────────────────
    try:
        positions = ig.get_positions()
        if positions:
            lines.append("🟢 Open Positions:")
            for p in positions:
                pos = p.get("position", {})
                mkt = p.get("market", {})
                name      = mkt.get("instrumentName", mkt.get("epic", "?"))
                direction = pos.get("direction", "?")
                size      = pos.get("size", "?")
                entry     = pos.get("openLevel", pos.get("level", "?"))
                upl       = pos.get("upl", "?")
                deal_id   = pos.get("dealId", "?")
                curr_price = mkt.get("bid", "?")
                lines.append(
                    f"  {name}\n"
                    f"  {direction} x{size}\n"
                    f"  Buy: ${entry}  Now: ${curr_price}  P&L: {upl}\n"
                    f"  Deal: {deal_id}"
                )
        else:
            lines.append("🟢 Open Positions: none")
    except Exception as e:
        lines.append(f"🟢 Open Positions: error — {e}")

    lines.append("")

    # ── Working (pending) orders ─────────────────────────────
    try:
        working = ig.get_working_orders()
        if working:
            lines.append("⏳ Pending Orders:")
            for w in working:
                wo  = w.get("workingOrder", {})
                mkt = w.get("market", {})
                name      = mkt.get("instrumentName", mkt.get("epic", "?"))
                direction = wo.get("direction", "?")
                size      = wo.get("size", "?")
                order_type = wo.get("orderType", "?")
                level     = wo.get("level", "?")
                deal_id   = wo.get("dealId", "?")
                lines.append(
                    f"  {name}\n"
                    f"  {direction} x{size} ({order_type}) @ ${level}\n"
                    f"  Deal: {deal_id}"
                )
        else:
            lines.append("⏳ Pending Orders: none")
    except Exception as e:
        lines.append(f"⏳ Pending Orders: error — {e}")

    tg_send("\n".join(lines))


# ================== ACCOUNT REPORT ==================
def send_account_report(broker):
    """Broker-agnostic account snapshot. Works with IGClient or AlpacaClient."""
    try:
        acc = broker.get_account()
        balance   = acc.get("balance", "N/A")
        available = acc.get("available", "N/A")
        deposit   = acc.get("deposit", "N/A")
        pnl       = acc.get("pnl", "N/A")
        currency  = acc.get("currency", "")
    except Exception as e:
        tg_send(f"⚠️ Could not fetch account info: {e}")
        return

    try:
        positions = broker.get_positions()
        pos_lines = []
        for p in positions:
            pos = p.get("position", {})
            mkt = p.get("market", {})
            pos_lines.append(
                f"  {mkt.get('instrumentName','?')} | {pos.get('direction','?')} "
                f"x{pos.get('size','?')} | P&L: {pos.get('upl','?')}"
            )
        pos_text = "\n".join(pos_lines) if pos_lines else "  No open positions"
    except Exception:
        pos_text = "  Could not fetch positions"

    msg = (
        f"📊 Account Report ({BROKER})\n"
        f"Balance:   {balance} {currency}\n"
        f"Available: {available} {currency}\n"
        f"Deposit:   {deposit} {currency}\n"
        f"P&L:       {pnl} {currency}\n"
        f"\nOpen Positions:\n{pos_text}"
    )
    tg_send(msg)
    logging.info("Account report sent")

# ================== CLAUDE TOP-LOSERS ANALYSIS ==================
_YF_SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
_YF_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?formatted=false&lang=en-US&region=US&count={count}&scrIds={scr_id}"
)


def _yf_screener(scr_id: str, n: int = 5):
    """Fetch a Yahoo Finance predefined screener. Returns list of quote dicts."""
    try:
        url = _YF_SCREENER_URL.format(count=max(n * 2, 20), scr_id=scr_id)
        r = requests.get(url, headers=_YF_SCREENER_HEADERS, timeout=10)
        r.raise_for_status()
        quotes = r.json()["finance"]["result"][0]["quotes"]
        return quotes[:n]
    except Exception as e:
        logging.error(f"YF screener {scr_id} error: {e}")
        return []


def _get_top_losers(n=10):
    """Top losers today from Yahoo Finance day_losers screener."""
    quotes = _yf_screener("day_losers", n)
    results = []
    for q in quotes:
        ticker = q.get("symbol", "")
        pct    = float(q.get("regularMarketChangePercent", 0))
        price  = float(q.get("regularMarketPrice", 0))
        results.append((ticker, pct, price))
    # fallback: compute from US_UNIVERSE if screener returned nothing
    if not results:
        raw = yf.download(US_UNIVERSE, period="2d", interval="1d", progress=False, group_by="ticker")
        for ticker in US_UNIVERSE:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    closes = raw[ticker]["Close"].dropna()
                else:
                    closes = raw["Close"].dropna()
                if len(closes) < 2:
                    continue
                prev_close  = float(closes.iloc[-2])
                today_close = float(closes.iloc[-1])
                pct = (today_close - prev_close) / prev_close * 100
                results.append((ticker, pct, today_close))
            except Exception:
                continue
        results.sort(key=lambda x: x[1])
        results = results[:n]
    return results


def _get_top_gainers(n=10):
    """Top gainers today from Yahoo Finance day_gainers screener."""
    quotes = _yf_screener("day_gainers", n)
    results = []
    for q in quotes:
        ticker = q.get("symbol", "")
        pct    = float(q.get("regularMarketChangePercent", 0))
        price  = float(q.get("regularMarketPrice", 0))
        results.append((ticker, pct, price))
    # fallback: compute from US_UNIVERSE if screener returned nothing
    if not results:
        raw = yf.download(US_UNIVERSE, period="2d", interval="1d", progress=False, group_by="ticker")
        for ticker in US_UNIVERSE:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    closes = raw[ticker]["Close"].dropna()
                else:
                    closes = raw["Close"].dropna()
                if len(closes) < 2:
                    continue
                prev_close  = float(closes.iloc[-2])
                today_close = float(closes.iloc[-1])
                pct = (today_close - prev_close) / prev_close * 100
                results.append((ticker, pct, today_close))
            except Exception:
                continue
        results.sort(key=lambda x: x[1], reverse=True)
        results = results[:n]
    return results


def _get_most_active(n=10):
    """Most active stocks today from Yahoo Finance most_actives screener."""
    quotes = _yf_screener("most_actives", n)
    results = []
    for q in quotes:
        ticker = q.get("symbol", "")
        pct    = float(q.get("regularMarketChangePercent", 0))
        price  = float(q.get("regularMarketPrice", 0))
        volume = int(q.get("regularMarketVolume", 0))
        results.append((ticker, pct, price, volume))
    return results


def _ichimoku_summary(ticker: str):
    """Return a brief Ichimoku signal string for a ticker (60 days, 1h bars)."""
    try:
        df = yf.download(ticker, period="60d", interval="1h", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 60:
            return "insufficient data"
        df = ichimoku(df)
        df.dropna(inplace=True)
        if df.empty:
            return "insufficient data after Ichimoku"
        last = df.iloc[-1]
        price = float(last["Close"])
        cloud_top = max(float(last["senkou_a"]), float(last["senkou_b"]))
        cloud_bot = min(float(last["senkou_a"]), float(last["senkou_b"]))
        above = price > cloud_top
        below = price < cloud_bot
        tenkan = float(last["tenkan"])
        kijun = float(last["kijun"])
        tk_bull = tenkan > kijun

        # Fibonacci from last 20 bars
        fib_0, fib_382, fib_618, fib_900 = build_fibonacci(df, len(df) - 1)

        signal = "above cloud" if above else ("below cloud" if below else "inside cloud")
        tk = "TK bullish" if tk_bull else "TK bearish"
        fib_zone = ""
        if above:
            if fib_618 < price <= fib_382:
                fib_zone = " | at 38.2% Fib (entry zone)"
            elif fib_900 < price <= fib_618:
                fib_zone = " | at 61.8% Fib (deeper entry zone)"

        return (f"{signal} | {tk}{fib_zone} | "
                f"Price={price:.2f} Cloud={cloud_bot:.2f}-{cloud_top:.2f} "
                f"Fib38={fib_382:.2f} Fib62={fib_618:.2f}")
    except Exception as e:
        return f"error: {e}"


CRYPTO_UNIVERSE = [
    "BTC-USD", "ETH-USD", "BNB-USD", "XRP-USD", "ADA-USD",
    "SOL-USD", "DOGE-USD", "DOT-USD", "AVAX-USD", "MATIC-USD",
    "LINK-USD", "LTC-USD", "ATOM-USD", "UNI-USD", "ETC-USD",
]


def _get_crypto_movers(n=10):
    """Top crypto gainers and losers from 24h change over CRYPTO_UNIVERSE."""
    raw = yf.download(CRYPTO_UNIVERSE, period="2d", interval="1d", progress=False, group_by="ticker")
    results = []
    for sym in CRYPTO_UNIVERSE:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw[sym]["Close"].dropna()
            else:
                closes = raw["Close"].dropna()
            if len(closes) < 2:
                continue
            prev_close  = float(closes.iloc[-2])
            today_close = float(closes.iloc[-1])
            pct = (today_close - prev_close) / prev_close * 100
            results.append((sym, pct, today_close))
        except Exception:
            continue
    gainers = sorted(results, key=lambda x: x[1], reverse=True)[:n]
    losers  = sorted(results, key=lambda x: x[1])[:n]
    return gainers, losers


def _fmt_price(p):
    return f"${p:.4f}" if p < 1 else f"${p:.2f}"


def analyze_crypto():
    """Read-only Claude crypto scan over CRYPTO_UNIVERSE — no auto-buy."""
    if not _ANTHROPIC_OK:
        tg_send("⚠️ anthropic package not installed.")
        return
    if not CLAUDE_API_KEY:
        tg_send("⚠️ CLAUDE_API_KEY not set in .env")
        return

    tg_send("🔍 Fetching crypto 24h movers...")
    try:
        gainers, losers = _get_crypto_movers(10)
    except Exception as e:
        tg_send(f"⚠️ Failed to fetch crypto data: {e}")
        return

    if not gainers and not losers:
        tg_send("No crypto data available.")
        return

    if gainers:
        lines = ["📈 Top Crypto Gainers (24h):"]
        for sym, pct, price in gainers:
            lines.append(f"  {sym}: {pct:+.2f}% @ {_fmt_price(price)}")
        tg_send("\n".join(lines))
    if losers:
        lines = ["📉 Top Crypto Losers (24h):"]
        for sym, pct, price in losers:
            lines.append(f"  {sym}: {pct:+.2f}% @ {_fmt_price(price)}")
        tg_send("\n".join(lines))

    tg_send("🤖 Running Ichimoku analysis & asking Claude...")

    analysis_sections = []
    for label, group in (("CRYPTO GAINERS", gainers), ("CRYPTO LOSERS", losers)):
        if not group:
            continue
        section_lines = []
        for sym, pct, price in group:
            summary = _ichimoku_summary(sym)
            section_lines.append(
                f"- {sym}: {pct:+.2f}% change, price {_fmt_price(price)}\n  Ichimoku: {summary}"
            )
        analysis_sections.append(f"{label}:\n" + "\n".join(section_lines))

    prompt = (
        "You are an expert crypto technical analyst using the Ichimoku + Fibonacci strategy.\n\n"
        "Here is today's crypto data with Ichimoku analysis:\n\n"
        + "\n\n".join(analysis_sections)
        + "\n\nFor each crypto in BOTH lists:\n"
        "1. Is there a valid long entry setup? (price above cloud, TK bullish, near 38.2% or 61.8% Fib)\n"
        "2. If yes, give entry zone, take-profit, and stop-loss levels.\n"
        "3. Rank the TOP 3 best trade opportunities across both lists by risk/reward.\n\n"
        "Be concise. Format for Telegram (max 4000 chars)."
    )

    try:
        client = _anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=2000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            msg = stream.get_final_message()
        reply = "".join(b.text for b in msg.content if b.type == "text")
        if reply:
            tg_send(f"🧠 Claude (Crypto):\n{reply[:4000]}")
        else:
            tg_send("Claude returned no text response.")
    except Exception as e:
        tg_send(f"⚠️ Claude API error: {e}")


def run_premarket_scan_once():
    """One-shot pre-market scan over US_UNIVERSE (4:00–9:30 AM ET window)."""
    try:
        import pytz
    except ImportError:
        tg_send("⚠️ pytz not installed. Run: pip install pytz")
        return
    from datetime import datetime as _dt
    ET = pytz.timezone("America/New_York")
    now_et = _dt.now(ET)
    hm = (now_et.hour, now_et.minute)
    if not ((4, 0) <= hm < (9, 30)):
        tg_send(
            f"⚠️ Outside pre-market window ({now_et.strftime('%I:%M %p ET')}).\n"
            f"Pre-market runs 4:00–9:30 AM ET."
        )
        return

    tg_send(f"🌅 Pre-market scan on {len(US_UNIVERSE)} tickers...")
    signals = []
    today = now_et.date()
    for sym in US_UNIVERSE:
        try:
            df = yf.download(sym, period="2d", interval="1m", prepost=True, progress=False)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.index = df.index.tz_convert(ET)
            df = df[df.index.date == today].between_time("04:00", "09:29")
            if len(df) < 10:
                continue
            green = int((df["Close"] > df["Open"]).sum())
            pct_green = green / len(df) * 100
            if pct_green < 60:
                continue
            daily = yf.download(sym, period="5d", interval="1d", progress=False)
            if daily.empty or len(daily) < 2:
                continue
            if isinstance(daily.columns, pd.MultiIndex):
                daily.columns = daily.columns.get_level_values(0)
            prev_close = float(daily["Close"].iloc[-2])
            last_price = float(df["Close"].iloc[-1])
            if prev_close <= 0:
                continue
            pct_change = (last_price / prev_close - 1) * 100
            if pct_change <= 0:
                continue
            signals.append((sym, pct_change, pct_green, last_price))
        except Exception:
            continue

    signals.sort(key=lambda x: (x[2], x[1]), reverse=True)
    if not signals:
        tg_send("🌅 Pre-market: no stocks with 60%+ bullish candles.")
        return
    lines = [f"🌅 Pre-market — {len(signals)} bullish stocks:"]
    for sym, pc, pg, price in signals[:15]:
        lines.append(f"  {sym}: +{pc:.2f}% @ ${price:.2f}  ({pg:.0f}% green)")
    tg_send("\n".join(lines))


def _force_close_all(ig):
    """Close every open position at market. No confirmation."""
    pos = _bot_state.get("open_positions", {})
    total = len(pos)
    if total == 0:
        tg_send("No open positions to close.")
        return
    tg_send(f"🚨 Force-closing {total} positions...")
    closed = 0
    for epic, data in list(pos.items()):
        deal_id, entry, tp, sl, sig, ticker = data
        try:
            ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
            sell_price = get_realtime_price(ticker) or entry
            pnl = sell_price - entry
            _bot_state["trade_log"].append(pnl)
            pos.pop(epic, None)
            tg_send(f"✅ Closed {ticker} ({sig}) P&L: {pnl:+.2f}")
            closed += 1
        except Exception as e:
            tg_send(f"❌ Close {ticker} failed: {e}")
    tg_send(f"Done. Closed {closed}/{total} positions.")


def _run_backtest(ticker):
    """Run a 3-year daily backtest for a single ticker; send summary to Telegram."""
    tg_send(f"📊 Running 3y backtest for {ticker}...")
    try:
        df = yf.download(ticker, period="3y", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty:
            tg_send(f"⚠️ No data for {ticker}.")
            return
        _, _, summary = backtest(df, plot=False)
        tg_send(f"📊 Backtest {ticker}\n{summary}")
    except Exception as e:
        tg_send(f"❌ Backtest failed: {e}")


def _check_price(ticker):
    """Cmd 19: quick price + 1d/5d change."""
    try:
        df = yf.download(ticker, period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 2:
            tg_send(f"⚠️ No data for {ticker}.")
            return
        last  = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2])
        first = float(df["Close"].iloc[0])
        pct_1d = (last / prev  - 1) * 100
        pct_5d = (last / first - 1) * 100
        tg_send(
            f"💲 {ticker}\n"
            f"  Price: ${last:.2f}\n"
            f"  1d: {pct_1d:+.2f}%\n"
            f"  5d: {pct_5d:+.2f}%"
        )
    except Exception as e:
        tg_send(f"❌ Price check failed: {e}")


def _analyze_single_ticker(ticker):
    """Cmd 20: Ichimoku summary + Claude opinion for a single ticker."""
    if not _ANTHROPIC_OK or not CLAUDE_API_KEY:
        tg_send("⚠️ Claude not configured.")
        return
    tg_send(f"🧠 Analyzing {ticker}...")
    try:
        summary = _ichimoku_summary(ticker)
        price = get_realtime_price(ticker)
        price_str = f"${price:.2f}" if price else "n/a"
        prompt = (
            f"You are an expert technical analyst using the Ichimoku + Fibonacci strategy.\n\n"
            f"Ticker: {ticker}\n"
            f"Current price: {price_str}\n"
            f"Ichimoku snapshot: {summary}\n\n"
            f"1. Is there a valid long entry setup right now?\n"
            f"2. If yes, give entry zone, take-profit, and stop-loss levels.\n"
            f"3. Confidence (low/medium/high) and one-sentence reasoning.\n\n"
            f"Be concise. Format for Telegram (max 2000 chars)."
        )
        client = _anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=1500,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
        reply = "".join(b.text for b in msg.content if b.type == "text")
        tg_send(f"🧠 {ticker} — Claude says:\n{reply[:3500]}" if reply else "Claude returned no text.")
    except Exception as e:
        tg_send(f"❌ Analysis failed: {e}")


def _refresh_epic_cache(broker):
    """Cmd 21: delete epic_cache.json and rebuild. No-op on Alpaca (no EPICs)."""
    if isinstance(broker, AlpacaClient):
        tg_send("ℹ️ Alpaca uses symbols directly — no EPIC cache to refresh.")
        return
    tg_send(f"🔄 Refreshing EPIC cache for {len(US_UNIVERSE)} tickers (~30s)...")
    try:
        if os.path.exists(EPIC_CACHE_FILE):
            os.remove(EPIC_CACHE_FILE)
        mapping = []
        for t in US_UNIVERSE:
            epic = get_epic(broker, t)
            if epic:
                mapping.append((t, epic))
            time.sleep(0.5)
        with open(EPIC_CACHE_FILE, "w") as f:
            json.dump(mapping, f)
        tg_send(f"✅ EPIC cache rebuilt: {len(mapping)}/{len(US_UNIVERSE)} resolved.")
    except Exception as e:
        tg_send(f"❌ Refresh failed: {e}")


SECTOR_ETFS = [
    ("XLK", "Technology"),
    ("XLF", "Financials"),
    ("XLE", "Energy"),
    ("XLV", "Healthcare"),
    ("XLY", "Cons. Discretionary"),
    ("XLP", "Cons. Staples"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLU", "Utilities"),
    ("XLRE", "Real Estate"),
    ("XLC", "Communication"),
]


def _sector_scan():
    """Cmd 23: today's % change for S&P sector ETFs."""
    tg_send("🔍 Fetching sector ETFs...")
    try:
        results = []
        for sym, name in SECTOR_ETFS:
            df = yf.download(sym, period="2d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.empty or len(df) < 2:
                continue
            prev = float(df["Close"].iloc[-2])
            last = float(df["Close"].iloc[-1])
            pct  = (last / prev - 1) * 100
            results.append((name, sym, pct, last))
        if not results:
            tg_send("No sector data available.")
            return
        results.sort(key=lambda x: x[2], reverse=True)
        lines = ["📊 Sector Scan (today):"]
        for name, sym, pct, last in results:
            arrow = "🟢" if pct > 0 else "🔴"
            lines.append(f"  {arrow} {name} ({sym}): {pct:+.2f}% @ ${last:.2f}")
        tg_send("\n".join(lines))
    except Exception as e:
        tg_send(f"❌ Sector scan failed: {e}")


def _get_news(ticker):
    """Cmd 24: recent headlines for a ticker via yfinance."""
    tg_send(f"📰 Fetching news for {ticker}...")
    try:
        items = yf.Ticker(ticker).news or []
        if not items:
            tg_send(f"No news for {ticker}.")
            return
        lines = [f"📰 {ticker} — recent headlines:"]
        for item in items[:5]:
            content = item.get("content", item)
            title = content.get("title") or item.get("title", "(no title)")
            pub   = content.get("pubDate") or item.get("providerPublishTime", "")
            lines.append(f"  • {title}\n    {pub}")
        tg_send("\n".join(lines))
    except Exception as e:
        tg_send(f"❌ News fetch failed: {e}")


def _export_trade_log_csv():
    """Cmd 26: dump trade_log and auto_buy_log to CSV files; send via Telegram."""
    try:
        import csv
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        # Trade log
        trade_path = f"trade_log_{stamp}.csv"
        with open(trade_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["index", "pnl"])
            for i, pnl in enumerate(_bot_state.get("trade_log", []), 1):
                w.writerow([i, pnl])
        tg_send_document(trade_path, caption=f"Trade log ({len(_bot_state.get('trade_log', []))} trades)")

        # Auto-buy log
        if _bot_state.get("auto_buy_log"):
            ab_path = f"auto_buy_log_{stamp}.csv"
            with open(ab_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "ticker", "entry", "tp", "sl", "deal_ref"])
                for e in _bot_state["auto_buy_log"]:
                    w.writerow([e["time"], e["ticker"], e["entry"], e["tp"], e["sl"], e["deal_ref"]])
            tg_send_document(ab_path, caption=f"Auto-buy log ({len(_bot_state['auto_buy_log'])} entries)")
    except Exception as e:
        tg_send(f"❌ CSV export failed: {e}")


def _risk_dashboard():
    """Cmd 27: drawdown, win rate, average win/loss, open exposure."""
    try:
        trades = _bot_state.get("trade_log", [])
        pos    = _bot_state.get("open_positions", {})
        if trades:
            n      = len(trades)
            wins   = [t for t in trades if t > 0]
            losses = [t for t in trades if t <= 0]
            wr     = len(wins) / n * 100 if n else 0
            avg_w  = sum(wins) / len(wins) if wins else 0
            avg_l  = sum(losses) / len(losses) if losses else 0
            # Drawdown over cumulative P&L curve
            cum, peak, max_dd = 0.0, 0.0, 0.0
            for t in trades:
                cum += t
                peak = max(peak, cum)
                max_dd = min(max_dd, cum - peak)
        else:
            n, wr, avg_w, avg_l, max_dd = 0, 0, 0, 0, 0
        # Open exposure
        exposure = 0.0
        for _, (_, entry, *_rest) in pos.items():
            exposure += entry * DEFAULT_SIZE
        tg_send(
            "📊 Risk Dashboard\n"
            f"Trades closed: {n}\n"
            f"Win rate: {wr:.1f}%\n"
            f"Avg win: {avg_w:+.2f}\n"
            f"Avg loss: {avg_l:+.2f}\n"
            f"Max drawdown: {max_dd:.2f}\n"
            f"Open positions: {len(pos)}\n"
            f"Exposure (est.): ${exposure:.2f}"
        )
    except Exception as e:
        tg_send(f"❌ Risk dashboard failed: {e}")


def _watch_list_loop():
    """Background thread: poll watch_list every 60s and alert on crossings."""
    while True:
        try:
            watches = list(_bot_state.get("watch_list", []))
            triggered_idx = []
            for i, w in enumerate(watches):
                price = get_realtime_price(w["ticker"])
                if price is None:
                    continue
                if (w["direction"] == "above" and price >= w["level"]) or \
                   (w["direction"] == "below" and price <= w["level"]):
                    tg_send(
                        f"🔔 ALERT {w['ticker']} {w['direction']} ${w['level']:.2f}\n"
                        f"   Now: ${price:.2f}"
                    )
                    triggered_idx.append(i)
            # Remove triggered watches
            if triggered_idx:
                wl = _bot_state.get("watch_list", [])
                for i in reversed(triggered_idx):
                    if i < len(wl):
                        wl.pop(i)
        except Exception as e:
            logging.error(f"Watch list loop error: {e}")
        time.sleep(60)


def analyze_top_losers():
    """Fetch top losers + gainers + most active, run Ichimoku on each, ask Claude for entry recommendations."""
    if not _ANTHROPIC_OK:
        tg_send("⚠️ anthropic package not installed. Run: pip install anthropic")
        return
    if not CLAUDE_API_KEY:
        tg_send("⚠️ CLAUDE_API_KEY not set in .env")
        return

    tg_send("🔍 Fetching today's top losers, gainers & most active from Yahoo Finance...")

    # ── Top losers ──────────────────────────────────────────
    try:
        losers = _get_top_losers(10)
    except Exception as e:
        tg_send(f"⚠️ Failed to fetch top losers: {e}")
        losers = []

    # ── Top gainers ─────────────────────────────────────────
    try:
        gainers = _get_top_gainers(10)
    except Exception as e:
        tg_send(f"⚠️ Failed to fetch top gainers: {e}")
        gainers = []

    # ── Most active ─────────────────────────────────────────
    try:
        actives = _get_most_active(10)
    except Exception as e:
        tg_send(f"⚠️ Failed to fetch most active: {e}")
        actives = []

    if not losers and not gainers and not actives:
        tg_send("No market data available today.")
        return

    # ── Send raw lists to Telegram ───────────────────────────
    if losers:
        lines = ["📉 Top 10 Losers today:"]
        for ticker, pct, price in losers:
            lines.append(f"  {ticker}: {pct:+.2f}% @ ${price:.2f}")
        tg_send("\n".join(lines))

    if gainers:
        lines = ["📈 Top 10 Gainers today:"]
        for ticker, pct, price in gainers:
            lines.append(f"  {ticker}: {pct:+.2f}% @ ${price:.2f}")
        tg_send("\n".join(lines))

    if actives:
        lines = ["🔥 Top 10 Most Active today:"]
        for ticker, pct, price, vol in actives:
            vol_str = f"{vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol/1_000:.0f}K"
            lines.append(f"  {ticker}: {pct:+.2f}% @ ${price:.2f}  Vol {vol_str}")
        tg_send("\n".join(lines))

    tg_send("🤖 Running Ichimoku analysis & asking Claude...")

    # ── Build analysis data for Claude ──────────────────────
    analysis_sections = []

    if losers:
        loser_lines = []
        for ticker, pct, price in losers:
            summary = _ichimoku_summary(ticker)
            loser_lines.append(
                f"- {ticker}: {pct:+.2f}% change, price ${price:.2f}\n  Ichimoku: {summary}"
            )
        analysis_sections.append(
            "TOP LOSERS:\n" + "\n".join(loser_lines)
        )

    if gainers:
        gainer_lines = []
        for ticker, pct, price in gainers:
            summary = _ichimoku_summary(ticker)
            gainer_lines.append(
                f"- {ticker}: {pct:+.2f}% change, price ${price:.2f}\n  Ichimoku: {summary}"
            )
        analysis_sections.append(
            "TOP GAINERS:\n" + "\n".join(gainer_lines)
        )

    if actives:
        active_lines = []
        for ticker, pct, price, vol in actives:
            summary = _ichimoku_summary(ticker)
            vol_str = f"{vol/1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol/1_000:.0f}K"
            active_lines.append(
                f"- {ticker}: {pct:+.2f}% change, price ${price:.2f}, volume {vol_str}\n  Ichimoku: {summary}"
            )
        analysis_sections.append(
            "MOST ACTIVE:\n" + "\n".join(active_lines)
        )

    prompt = (
        "You are an expert technical analyst using the Ichimoku + Fibonacci strategy.\n\n"
        "Here is today's market data with Ichimoku analysis:\n\n"
        + "\n\n".join(analysis_sections)
        + "\n\nFor each ticker in ALL lists:\n"
        "1. Is there a valid long entry setup? (price above cloud, TK bullish, near 38.2% or 61.8% Fib)\n"
        "2. If yes, give entry zone, take-profit, and stop-loss levels.\n"
        "3. At the end, rank the TOP 3 best trade opportunities across all lists by risk/reward.\n\n"
        "Be concise. Format for Telegram (max 4000 chars).\n\n"
        "IMPORTANT: After your analysis, append a JSON code block with the top picks for AUTO-BUY. "
        "Include ONLY tickers with a clean long setup (above cloud, TK bullish, at Fib entry zone). "
        "Use an empty array if no picks qualify. Use plain numbers (no $ signs, no commas):\n\n"
        "```json\n"
        "{\"top_picks\": [\n"
        "  {\"ticker\": \"AAPL\", \"entry\": 195.50, \"tp\": 210.00, \"sl\": 188.00}\n"
        "]}\n"
        "```"
    )

    reply = ""
    try:
        client = _anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=2500,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            msg = stream.get_final_message()

        for block in msg.content:
            if block.type == "text":
                reply += block.text
    except Exception as e:
        tg_send(f"⚠️ Claude API error: {e}")
        return

    if not reply:
        tg_send("Claude returned no text response.")
        return

    # ── Parse top picks JSON ─────────────────────────────────
    top_picks = []
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", reply, re.DOTALL)
    if json_match:
        try:
            top_picks = json.loads(json_match.group(1)).get("top_picks", []) or []
        except json.JSONDecodeError as e:
            logging.error(f"Top picks JSON parse failed: {e}")

    # Strip JSON block from human-readable analysis
    display_reply = re.sub(r"```json.*?```", "", reply, flags=re.DOTALL).strip()
    tg_send(f"🧠 Claude Analysis:\n{display_reply[:4000]}")

    # ── Auto-buy Claude's top picks ──────────────────────────
    if not _bot_state.get("auto_buy_enabled", True):
        tg_send("ℹ️ Auto-buy is OFF — skipping order placement. Use cmd 25 to toggle.")
        return
    if not top_picks:
        tg_send("ℹ️ No auto-buy: Claude found no qualifying setups.")
        return

    ig = _bot_state.get("ig")
    if not ig:
        tg_send("⚠️ Auto-buy skipped: IG not connected.")
        return

    open_positions = _bot_state.setdefault("open_positions", {})
    placed = 0
    for pick in top_picks[:3]:
        try:
            ticker = str(pick.get("ticker", "")).upper().strip()
            entry  = float(pick["entry"])
            tp     = float(pick["tp"])
            sl     = float(pick["sl"])
        except (KeyError, TypeError, ValueError):
            tg_send(f"⚠️ Skip malformed pick: {pick}")
            continue
        if not ticker:
            continue

        epic = get_epic(ig, ticker)
        if not epic:
            tg_send(f"⚠️ Skip {ticker}: no IG EPIC available.")
            continue
        if epic in open_positions:
            tg_send(f"⚠️ Skip {ticker}: already have an open position.")
            continue

        try:
            res = ig.place_order(epic, "BUY", DEFAULT_SIZE)
            deal_ref = res.get("dealReference", "N/A")
            open_positions[epic] = (deal_ref, entry, tp, sl, "AUTO-BUY", ticker)
            _bot_state.setdefault("auto_buy_log", []).append({
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "ticker": ticker,
                "entry": entry,
                "tp": tp,
                "sl": sl,
                "deal_ref": deal_ref,
            })
            tg_send(
                f"🤖 AUTO-BUY {ticker} x{DEFAULT_SIZE}\n"
                f"📥 Entry: ${entry:.2f}\n"
                f"🎯 TP:    ${tp:.2f}\n"
                f"🛑 SL:    ${sl:.2f}\n"
                f"Ref: {deal_ref}"
            )
            placed += 1
        except Exception as e:
            tg_send(f"❌ Auto-buy {ticker} failed: {e}")

    if placed == 0:
        tg_send("ℹ️ No auto-buys placed (all picks skipped).")


# ================== LIVE BOT (YAHOO PRICE FOR ENTRY & EXIT) ==================
def live_trading_all_us_stocks():
    if BROKER == "ALPACA":
        ig = AlpacaClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        broker_label = f"Alpaca ({'paper' if ALPACA_PAPER else 'LIVE'})"
    else:
        ig = IGClient(IG_API_KEY, IG_IDENTIFIER, IG_PASSWORD, IG_ACCOUNT_ID, demo=IG_DEMO)
        broker_label = f"IG ({'demo' if IG_DEMO else 'LIVE'})"
    _bot_state["ig"] = ig
    _bot_state["start_time"] = time.time()

    threading.Thread(target=tg_listener, daemon=True).start()
    threading.Thread(target=_watch_list_loop, daemon=True).start()

    tg_send(f"🤖 Bot Started — broker: {broker_label} (Top 30 US Stocks)")
    tg_send(MENU)

    mapping = build_epic_universe(ig)  # list of (ticker, epic)
    tg_send(f"Tracking {len(mapping)} EPICs")
    send_account_report(ig)

    # epic -> (deal_id, entry_price, tp, sl, type, ticker)
    open_positions = {}
    _bot_state["open_positions"] = open_positions
    last_report_time = time.time()
    sent_premarket_date  = None
    sent_postmarket_date = None

    while True:
        try:
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            today   = now_utc.date()

            # Pre-market report: 9:15 AM ET = 13:15 UTC
            if (now_utc.hour, now_utc.minute) >= (13, 15) and sent_premarket_date != today:
                send_account_report(ig)
                tg_send("🔔 Pre-Market Report (US market opens at 9:30 AM ET)")
                sent_premarket_date = today
                last_report_time = time.time()

            # Post-market report: 4:15 PM ET = 20:15 UTC
            if (now_utc.hour, now_utc.minute) >= (20, 15) and sent_postmarket_date != today:
                send_account_report(ig)
                tg_send("🔔 Post-Market Report (US market closed at 4:00 PM ET)")
                sent_postmarket_date = today
                last_report_time = time.time()

            # Hourly report (only if pre/post market didn't already fire)
            if time.time() - last_report_time >= 3600:
                send_account_report(ig)
                last_report_time = time.time()

            if not _bot_state.get("running", True):
                time.sleep(10)
                continue

            signals = scan_all_markets(ig, mapping)
            _bot_state["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")

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
                    data = ig.prices(epic, "H1", 2)
                    df = ig_prices_to_df(data)
                    if df.empty:
                        continue
                    last_price = df['Close'].iloc[-1]

                if last_price >= tp:
                    ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
                    pnl = tp - entry_price
                    _bot_state["trade_log"].append(pnl)
                    msg = f"✅ TP {epic} ({ticker}) {sig_type} PnL={pnl:.2f}"
                    tg_send(msg)
                    logging.info(msg)
                    del open_positions[epic]

                elif last_price <= sl:
                    ig.close_position(deal_id, "SELL", DEFAULT_SIZE)
                    pnl = sl - entry_price
                    _bot_state["trade_log"].append(pnl)
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
