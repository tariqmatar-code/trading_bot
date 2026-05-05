"""
Claude AI analyst — analyses pre-market signals and returns a verdict + reasoning.
"""

import json
import requests

import config
from utils import log


def analyse_premarket_signal(signal: dict, df) -> dict:
    """
    Sends pre-market signal data to Claude for analysis.
    Returns dict with keys: verdict ("BUY" / "SKIP"), reason (str).
    Falls back to {"verdict": "BUY", "reason": "AI unavailable"} on error.
    """
    if not config.CLAUDE_API_KEY or "YOUR_" in config.CLAUDE_API_KEY:
        return {"verdict": "BUY", "reason": "Claude API key not configured"}

    try:
        # Last 10 pre-market candles as context
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        recent = df.tail(10)[cols].round(4).to_dict(orient="records")

        prompt = f"""You are an expert pre-market stock analyst.

A stock has been flagged by a pre-market scanner with the following data:
- Symbol: {signal['symbol']}
- Current pre-market price: ${signal['last_price']:.2f}
- Change vs yesterday's close: +{signal['pct_change']:.2f}%
- Bullish (green) candles: {signal['pct_green']:.0f}% of {signal['total_candles']} candles
- Pre-market volume: {signal['volume']:,}

Last 10 pre-market 1-minute candles (oldest to newest):
{json.dumps(recent, indent=2)}

Based on this pre-market data, assess whether this stock is worth watching for a potential long trade at market open.

Respond in this exact format:
VERDICT: BUY or SKIP
REASON: one sentence explanation"""

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        response.raise_for_status()
        text = response.json()["content"][0]["text"].strip()

        verdict = "BUY"
        reason  = "Strong pre-market momentum"
        for line in text.splitlines():
            if line.upper().startswith("VERDICT:"):
                v = line.split(":", 1)[1].strip().upper()
                verdict = "BUY" if "BUY" in v else "SKIP"
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        return {"verdict": verdict, "reason": reason}

    except Exception as e:
        log.warning(f"Claude analysis failed for {signal['symbol']}: {e}")
        return {"verdict": "BUY", "reason": "AI analysis unavailable"}
