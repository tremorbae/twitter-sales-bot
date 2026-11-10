"""Turns raw payment amounts into human prices and USD values."""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

import requests

from chain import RPC, decode_string

log = logging.getLogger(__name__)

COINBASE_URL = "https://api.coinbase.com/v2/prices/{}-USD/spot"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=usd"
COINGECKO_IDS = {"MNT": "mantle"}  # coins Coinbase lists under another meaning of the same ticker


class Pricer:
    def __init__(self, rpc: RPC, payment_tokens: dict):
        self.rpc = rpc
        self.tokens = dict(payment_tokens)
        self._usd: Dict[str, Tuple[Optional[Decimal], float]] = {}  # coin -> (price, when fetched)

    def token_info(self, token: str) -> dict:
        if token not in self.tokens:
            # A token we don't know yet: ask the contract. No USD value for these.
            try:
                symbol = decode_string(self.rpc.eth_call(token, "0x95d89b41"))
                decimals = int(self.rpc.eth_call(token, "0x313ce567"), 16)
            except Exception:
                symbol, decimals = "TOKENS", 18
            self.tokens[token] = {"symbol": symbol, "decimals": decimals, "peg": None}
        return self.tokens[token]

    def usd_price(self, coin: str) -> Optional[Decimal]:
        """USD price of a coin like ETH or BERA, refreshed at most once a minute."""
        price, fetched = self._usd.get(coin, (None, 0.0))
        if time.time() - fetched > 60:
            try:
                if coin in COINGECKO_IDS:
                    resp = requests.get(COINGECKO_URL.format(COINGECKO_IDS[coin]), timeout=10)
                    resp.raise_for_status()
                    price = Decimal(str(resp.json()[COINGECKO_IDS[coin]]["usd"]))
                else:
                    resp = requests.get(COINBASE_URL.format(coin), timeout=10)
                    resp.raise_for_status()
                    price = Decimal(resp.json()["data"]["amount"])
                self._usd[coin] = (price, time.time())
            except Exception as e:
                log.warning("Couldn't fetch the %s price (%s); using the last known price", coin, e)
        return price

    def describe(self, token: str, amount: int) -> Tuple[Decimal, str, Optional[Decimal]]:
        """Returns (price, symbol, usd) for a raw on-chain amount."""
        info = self.token_info(token)
        price = Decimal(amount) / (Decimal(10) ** info["decimals"])
        usd = None
        if info["peg"] == "USD":
            usd = price
        elif info["peg"]:
            rate = self.usd_price(info["peg"])
            usd = price * rate if rate is not None else None
        return price, info["symbol"], usd
