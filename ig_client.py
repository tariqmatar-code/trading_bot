"""
Simple IG Markets REST API client.
Handles session management and common API calls.
"""

import requests
import config


class IGClient:
    def __init__(self):
        acc_type = config.IG_ACCOUNT_TYPE.strip().upper()
        self.base_url = (
            "https://demo-api.ig.com/gateway/deal"
            if acc_type == "DEMO"
            else "https://api.ig.com/gateway/deal"
        )
        self.api_key = config.IG_API_KEY.strip()
        self.identifier = config.IG_IDENTIFIER.strip()
        self.password = config.IG_PASSWORD.strip()
        self.cst = None
        self.security_token = None
        self.account_id = None

    def login(self) -> None:
        r = requests.post(
            f"{self.base_url}/session",
            json={"identifier": self.identifier, "password": self.password},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json; charset=UTF-8",
                "X-IG-API-KEY": self.api_key,
                "Version": "2",
            },
            timeout=15,
        )
        r.raise_for_status()
        self.cst = r.headers["CST"]
        self.security_token = r.headers["X-SECURITY-TOKEN"]
        self.account_id = r.json().get("currentAccountId", "")

    def _headers(self, version: str = "1") -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self.api_key,
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token,
            "Version": version,
        }
        if self.account_id:
            h["IG-ACCOUNT-ID"] = self.account_id
        return h

    def get(self, endpoint: str, version: str = "1", params: dict = None) -> dict:
        r = requests.get(
            f"{self.base_url}/{endpoint}",
            headers=self._headers(version),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def post(self, endpoint: str, body: dict, version: str = "1") -> dict:
        r = requests.post(
            f"{self.base_url}/{endpoint}",
            json=body,
            headers=self._headers(version),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # --- Account ---

    def get_accounts(self) -> dict:
        return self.get("accounts")

    def get_preferred_account(self) -> dict:
        data = self.get_accounts()
        for acc in data.get("accounts", []):
            if acc.get("preferred"):
                return acc
        return data["accounts"][0]

    # --- Positions ---

    def get_positions(self) -> list:
        for version in ("2", "1"):
            try:
                return self.get("positions/otc", version=version).get("positions", [])
            except Exception:
                continue
        # Some demo accounts don't support /positions/otc — return empty list
        return []

    def get_position_for_epic(self, epic: str) -> dict | None:
        for p in self.get_positions():
            if p["market"]["epic"] == epic:
                return p
        return None

    # --- Market ---

    def get_market(self, epic: str) -> dict:
        return self.get(f"markets/{epic}", version="3")

    def is_market_open(self, epic: str) -> bool:
        market = self.get_market(epic)
        return market["snapshot"]["marketStatus"] == "TRADEABLE"

    def get_current_mid_price(self, epic: str) -> float:
        snap = self.get_market(epic)["snapshot"]
        return (float(snap["bid"]) + float(snap["offer"])) / 2

    def search_markets(self, search_term: str) -> list:
        return self.get("markets", params={"searchTerm": search_term}).get("markets", [])

    def find_epic_for_ticker(self, ticker: str) -> str | None:
        results = self.search_markets(ticker)
        for market in results:
            if (
                market.get("instrumentType") == "SHARES"
                and ticker.upper() in market.get("epic", "").upper()
            ):
                return market["epic"]
        return results[0]["epic"] if results else None

    # --- Price history ---

    def get_prices(self, epic: str, resolution: str = "MINUTE", max_points: int = 150) -> list:
        data = self.get(
            f"prices/{epic}",
            version="3",
            params={"resolution": resolution, "max": max_points},
        )
        return data.get("prices", [])

    # --- Trading ---

    def confirm_deal(self, deal_reference: str) -> dict:
        return self.get(f"confirms/{deal_reference}")

    def open_position(self, direction: str, epic: str, size: float, expiry: str = "-") -> dict:
        acc = self.get_preferred_account()
        body = {
            "epic": epic,
            "expiry": expiry,
            "direction": direction,
            "size": str(size),
            "orderType": "MARKET",
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": acc.get("currency", "USD"),
        }
        result = self.post("positions/otc", body, version="2")
        return self.confirm_deal(result["dealReference"])

    def close_position(self, deal_id: str, direction: str, epic: str = None, size: float = None, expiry: str = "-") -> dict:
        headers = self._headers("1")
        headers["_method"] = "DELETE"
        r = requests.post(
            f"{self.base_url}/positions/otc",
            json={"dealId": deal_id, "direction": direction, "size": str(size), "orderType": "MARKET"},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        return self.confirm_deal(r.json()["dealReference"])

    # --- Transaction history ---

    def get_transactions(self, max_span_seconds: int = 86400) -> list:
        return self.get(
            "history/transactions",
            version="2",
            params={"type": "ALL_DEAL", "maxSpanSeconds": max_span_seconds},
        ).get("transactions", [])
