"""
=============================================================================
  Trading Bot Configuration - IG Markets
=============================================================================
  Copy .env.example to .env and fill in your credentials.
  Never commit .env to version control.
=============================================================================
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# IG MARKETS API
# ---------------------------------------------------------------------------
IG_API_KEY      = os.environ["IG_API_KEY"]
IG_IDENTIFIER   = os.environ["IG_IDENTIFIER"]
IG_PASSWORD     = os.environ["IG_PASSWORD"]
IG_ACCOUNT_TYPE = os.getenv("IG_ACCOUNT_TYPE", "DEMO")

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# ---------------------------------------------------------------------------
# CLAUDE AI (pre-market signal analysis)
# ---------------------------------------------------------------------------
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

# ---------------------------------------------------------------------------
# TWELVE DATA API (price data source)
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TWELVE_DATA_SYMBOL  = os.getenv("TWELVE_DATA_SYMBOL", "SPY")

# ---------------------------------------------------------------------------
# TRADING SETTINGS
# ---------------------------------------------------------------------------
IG_EPIC             = os.getenv("IG_EPIC", "IX.D.SPTRD.IFM.IP")
IG_EXPIRY           = os.getenv("IG_EXPIRY", "-")
POSITION_SIZE_PCT   = int(os.getenv("POSITION_SIZE_PCT", "10"))
SHORT_MA            = int(os.getenv("SHORT_MA", "20"))
LONG_MA             = int(os.getenv("LONG_MA", "50"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

# ---------------------------------------------------------------------------
# SCANNER SETTINGS
# ---------------------------------------------------------------------------
SCANNER_UNIVERSE          = os.getenv("SCANNER_UNIVERSE", "SP500")
RSI_OVERSOLD              = int(os.getenv("RSI_OVERSOLD", "30"))
MIN_PCT_DROP              = float(os.getenv("MIN_PCT_DROP", "3.0"))
MIN_VOLUME                = int(os.getenv("MIN_VOLUME", "500000"))
AUTO_BUY_TIMEOUT_SECONDS  = int(os.getenv("AUTO_BUY_TIMEOUT_SECONDS", "300"))
SCANNER_POSITION_SIZE_PCT = int(os.getenv("SCANNER_POSITION_SIZE_PCT", "5"))
