"""
Shared utilities: config validation, IG connection test, Telegram messaging, alerts, logging.
"""

import logging
import sys
from pathlib import Path

import requests

import config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def validate_config() -> None:
    errors = []

    key = config.IG_API_KEY.strip()
    if not key or "YOUR_IG" in key:
        errors.append("IG_API_KEY is not set. Get it from My IG > Settings > API Management.")

    ident = config.IG_IDENTIFIER.strip()
    if not ident or "YOUR_IG" in ident:
        errors.append("IG_IDENTIFIER is not set. Use your IG username or account number.")

    pwd = config.IG_PASSWORD.strip()
    if not pwd or "YOUR_IG" in pwd:
        errors.append("IG_PASSWORD is not set.")

    acc_type = config.IG_ACCOUNT_TYPE.strip().upper()
    if acc_type not in ("DEMO", "LIVE"):
        errors.append(f"IG_ACCOUNT_TYPE must be 'DEMO' or 'LIVE'. Got: '{acc_type}'.")

    epic = config.IG_EPIC.strip()
    if not epic or "YOUR" in epic:
        errors.append("IG_EPIC is not set. Example: 'IX.D.SPTRD.DAILY.IP' for S&P 500.")

    token = config.TELEGRAM_BOT_TOKEN.strip()
    if not token or "REPLACE_WITH" in token or ":" not in token:
        errors.append("TELEGRAM_BOT_TOKEN is not set or invalid. Get it from @BotFather.")

    chat_id = config.TELEGRAM_CHAT_ID.strip()
    if not chat_id or "REPLACE_WITH" in chat_id or not chat_id.lstrip("-").isdigit():
        errors.append(f"TELEGRAM_CHAT_ID is not set or invalid. Got: '{chat_id}'.")

    if errors:
        print("\n" + "=" * 70)
        print("  CONFIG ERRORS - bot cannot start")
        print("=" * 70)
        for i, err in enumerate(errors, 1):
            print(f"\n  {i}. {err}")
        print("\n" + "=" * 70 + "\n")
        sys.exit(1)


def test_ig_connection() -> bool:
    try:
        from ig_client import IGClient
        client = IGClient()
        client.login()
        acc = client.get_preferred_account()
        balance = float(acc["balance"]["balance"])
        currency = acc["currency"]
        log.info(f"IG connection OK. Balance: {currency} {balance:,.2f} ({config.IG_ACCOUNT_TYPE})")
        return True
    except Exception as e:
        msg = str(e).lower()
        print("\n" + "=" * 70)
        print("  IG CONNECTION FAILED")
        print("=" * 70)
        if "401" in msg or "403" in msg or "unauthorized" in msg or "invalid" in msg:
            print("""
  Your IG credentials were rejected.

  Check:
    1. IG_API_KEY    — from My IG > Settings > API Management
    2. IG_IDENTIFIER — your IG username or account number
    3. IG_PASSWORD   — your IG account password
    4. IG_ACCOUNT_TYPE — must match the key type ("DEMO" or "LIVE")
       Demo keys only work against the demo API and vice versa.
""")
        else:
            print(f"\n  Error: {e}\n")
        print("=" * 70 + "\n")
        return False


def test_telegram_connection() -> bool:
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN.strip()}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID.strip(),
            "text": "✅ Telegram connection test successful",
        }
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("ok"):
            log.info("Telegram connection OK")
            return True
        else:
            print("\n" + "=" * 70)
            print("  TELEGRAM CONNECTION FAILED")
            print("=" * 70)
            error_desc = data.get("description", "unknown error")
            print(f"\n  Telegram says: {error_desc}\n")
            if "chat not found" in error_desc.lower():
                print("  Fix: Open your bot in Telegram and send it any message, then run again.\n")
            elif "unauthorized" in error_desc.lower() or "not found" in error_desc.lower():
                print("  Fix: Get a fresh token from @BotFather (/mybots) and update config.py.\n")
            print("=" * 70 + "\n")
            return False
    except Exception as e:
        log.error(f"Telegram test failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Telegram messaging
# ---------------------------------------------------------------------------
def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN.strip()}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID.strip(),
            "text": message,
            "parse_mode": parse_mode,
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.json().get("ok", False)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def send_telegram_document(file_path: Path, caption: str = "") -> bool:
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN.strip()}/sendDocument"
        with open(file_path, "rb") as f:
            response = requests.post(
                url,
                data={"chat_id": config.TELEGRAM_CHAT_ID.strip(), "caption": caption, "parse_mode": "HTML"},
                files={"document": f},
                timeout=30,
            )
            return response.json().get("ok", False)
    except Exception as e:
        log.warning(f"Telegram document send failed: {e}")
        return False


def alert(message: str, level: str = "info") -> None:
    if level == "error":
        log.error(message)
    elif level == "warning":
        log.warning(message)
    else:
        log.info(message)
    send_telegram(message)
