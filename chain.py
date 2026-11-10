"""Reads the chain over JSON-RPC and turns NFT transfers into sales."""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Dict, List, NamedTuple, Optional, Tuple

import requests

from config import (BLUR_EXCHANGE, NATIVE, OPENSEA_FEE_RECIPIENTS, OPENSEA_ZONES, SCATTER_OFFERERS, SCATTER_ROUTERS,
                    SEAPORT_ADDRESSES, Chain)

log = logging.getLogger(__name__)

USER_AGENT = "nft-sales-bot/1.0"  # the Robinhood RPC rejects some default client user agents

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ORDER_FULFILLED_TOPIC = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"
BLUR_EXECUTION_TOPIC = "0x1d5e12b51dee5e4d34434576c3fb99714a85f57b0fd546ada4b0bddd736d12b2"  # Execution721Packed
BURN_ADDRESSES = {NATIVE, "0x000000000000000000000000000000000000dead"}

# How RPCs word "this eth_getLogs request covers too many blocks or logs" (it varies by provider).
TOO_MUCH_DATA = re.compile(r"range|results|response size|too (large|wide|many (logs|results|blocks))|narrow|timeout", re.I)

# Seaport item types
NATIVE_ITEM, ERC20_ITEM, ERC721_ITEM, ERC721_CRITERIA_ITEM = 0, 1, 2, 4


class RPCError(Exception):
    pass


class RPC:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def call(self, method: str, params: list):
        error: Exception = RPCError(method)
        for attempt in range(4):
            if attempt:
                log.warning("RPC %s failed (%s), retrying", method, error)
                time.sleep(2 ** (attempt - 1))
            try:
                resp = self.session.post(
                    self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30
                )
            except requests.RequestException as e:
                error = e
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            busy = resp.status_code == 429 or resp.status_code >= 500
            if isinstance(body, dict) and "error" in body and not busy:
                # Some RPCs send errors with an HTTP error status too; the message says what's wrong.
                raise RPCError(f"{method}: {body['error']}")
            if resp.ok and isinstance(body, dict):
                return body["result"]
            error = RPCError(f"{method}: HTTP {resp.status_code} {resp.text[:200].strip()}")
            if not busy:
                raise error  # e.g. the RPC blocked the request: retrying won't help
        raise error

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def get_logs(self, address: str, topics: list, from_block: int, to_block: int) -> list:
        try:
            return self.call(
                "eth_getLogs",
                [{"address": address, "topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}],
            )
        except RPCError as e:
            if from_block >= to_block or not TOO_MUCH_DATA.search(str(e)):
                raise
            # The RPC limits how many blocks or logs one request covers: split the range in half.
            mid = (from_block + to_block) // 2
            return self.get_logs(address, topics, from_block, mid) + self.get_logs(address, topics, mid + 1, to_block)

    def receipt(self, tx_hash: str) -> dict:
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def transaction(self, tx_hash: str) -> dict:
        return self.call("eth_getTransactionByHash", [tx_hash])

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])


# ---- ABI helpers -----------------------------------------------------------------

def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_string(result: str) -> str:
    """Decode an ABI-encoded `string` return value (also handles old bytes32 returns)."""
    raw = bytes.fromhex(result[2:])
    if len(raw) == 32:
        return raw.rstrip(b"\0").decode("utf-8", "replace")
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset:offset + 32], "big")
    return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace")


def encode_uint(value: int) -> str:
    return format(value, "064x")


# ---- Seaport ---------------------------------------------------------------------

class Item(NamedTuple):
    item_type: int
    token: str
    identifier: int
    amount: int
    recipient: Optional[str] = None  # only set on consideration items


class Order(NamedTuple):
    offerer: str
    offer: List[Item]
    consideration: List[Item]
    zone: str = ""


def decode_order_fulfilled(log_entry: dict) -> Order:
    """Decode Seaport's OrderFulfilled(orderHash, offerer, zone, recipient, offer[], consideration[])."""
    data = bytes.fromhex(log_entry["data"][2:])
    words = [data[i:i + 32] for i in range(0, len(data), 32)]
    uint = lambda w: int.from_bytes(w, "big")
    addr = lambda w: "0x" + w[12:].hex()

    offer_at, consideration_at = uint(words[2]) // 32, uint(words[3]) // 32
    offer = []
    for i in range(uint(words[offer_at])):
        base = offer_at + 1 + i * 4
        offer.append(Item(uint(words[base]), addr(words[base + 1]), uint(words[base + 2]), uint(words[base + 3])))
    consideration = []
    for i in range(uint(words[consideration_at])):
        base = consideration_at + 1 + i * 5
        consideration.append(Item(
            uint(words[base]), addr(words[base + 1]), uint(words[base + 2]), uint(words[base + 3]), addr(words[base + 4])
        ))
    topics = log_entry["topics"]
    return Order(topic_to_address(topics[1]), offer, consideration, topic_to_address(topics[2]))


def _is_nft(item: Item) -> bool:
    return item.item_type in (ERC721_ITEM, ERC721_CRITERIA_ITEM)


def _payment(items: List[Item]) -> Optional[Tuple[str, int]]:
    """Total paid in the first payment token found among `items`."""
    payments = [i for i in items if i.item_type in (NATIVE_ITEM, ERC20_ITEM)]
    if not payments:
        return None
    token = payments[0].token
    return token, sum(i.amount for i in payments if i.token == token)


def seaport_price(orders: List[Order], contract: str, token_id: int) -> Optional[Tuple[str, int]]:
    """Gross price paid for one NFT, from the Seaport orders in its transaction.

    A bought listing has the NFT in `offer` and payments in `consideration`; an
    accepted bid is the reverse. Accepted bids are often settled against a
    "mirror" order that only pays the seller's cut after fees, so when both
    sides are present the larger (gross) amount is the price.
    """
    best = None
    for order in orders:
        for nft_side, pay_side in ((order.offer, order.consideration), (order.consideration, order.offer)):
            nfts = [i for i in nft_side if _is_nft(i)]
            if not any(i.token == contract and i.identifier == token_id for i in nfts):
                continue
            paid = _payment(pay_side)
            if paid:
                per_item = paid[1] // len(nfts)  # bundles: split evenly
                if best is None or per_item > best[1]:
                    best = (paid[0], per_item)
    return best


def seaport_marketplace(orders: List[Order], contract: str, token_id: int) -> Optional[str]:
    """Which marketplace the listing or offer behind this NFT's Seaport trade came from.

    Only this NFT's own orders count, so a sweep mixing OpenSea and Scatter listings
    labels each NFT correctly. None when no marketplace can be confirmed.
    """
    mine = [o for o in orders
            if any(_is_nft(i) and i.token == contract and i.identifier == token_id for i in o.offer + o.consideration)]
    if any(o.offerer in SCATTER_OFFERERS for o in mine):
        return "Scatter"  # Scatter listings and offers are orders from its own contract
    if any(o.zone in OPENSEA_ZONES or any(i.recipient in OPENSEA_FEE_RECIPIENTS for i in o.consideration) for o in mine):
        return "OpenSea"  # only OpenSea's orders pay OpenSea's fee or need its signed zone's approval
    return None


# ---- Blur ------------------------------------------------------------------------

def blur_prices(receipt: dict) -> Dict[Tuple[str, int], int]:
    """ETH paid per NFT in a transaction's Blur trades, as {(collection, token_id): wei}.

    Blur logs one Execution721Packed event per NFT, with two packed words:
    tokenId | listingIndex | trader, and side | price | collection.
    """
    prices = {}
    for entry in receipt["logs"]:
        if entry["address"].lower() == BLUR_EXCHANGE and entry["topics"][0] == BLUR_EXECUTION_TOPIC:
            data = entry["data"][2:]
            token_word, collection_word = int(data[64:128], 16), int(data[128:192], 16)
            collection = "0x%040x" % (collection_word & (2 ** 160 - 1))
            prices[(collection, token_word >> 168)] = (collection_word >> 160) & (2 ** 88 - 1)
    return prices


# ---- Sales -------------------------------------------------------------------------

@dataclass
class Transfer:
    tx_hash: str
    log_index: int
    block: int
    token_id: int
    seller: str
    buyer: str


@dataclass
class Sale:
    tx_hash: str
    log_index: int
    block: int
    token_id: int
    seller: str
    buyer: str
    payment_token: str  # NATIVE for plain ETH
    amount: int         # in the payment token's smallest unit
    marketplace: Optional[str]

    @property
    def id(self) -> str:
        return f"{self.tx_hash}:{self.log_index}"


def nft_transfers(logs: list) -> List[Transfer]:
    transfers = []
    for entry in logs:
        topics = entry["topics"]
        # ERC-721 Transfer has 4 topics; the 3-topic version is an ERC-20 transfer.
        if entry.get("removed") or len(topics) != 4 or topics[0] != TRANSFER_TOPIC:
            continue
        seller, buyer = topic_to_address(topics[1]), topic_to_address(topics[2])
        if seller == NATIVE or buyer in BURN_ADDRESSES:
            continue  # mint or burn
        transfers.append(Transfer(
            entry["transactionHash"], int(entry["logIndex"], 16), int(entry["blockNumber"], 16),
            int(topics[3], 16), seller, buyer,
        ))
    return transfers


def _merge_hops(transfers: List[Transfer]) -> List[Transfer]:
    """A token can pass through a router or smart wallet within one transaction.
    Treat that as a single move from the first sender to the final owner."""
    merged: "OrderedDict[int, Transfer]" = OrderedDict()
    for t in transfers:
        if t.token_id in merged:
            merged[t.token_id].buyer = t.buyer
        else:
            merged[t.token_id] = replace(t)
    return [t for t in merged.values() if t.seller != t.buyer]


def _direct_payment(transfer: Transfer, transfers: List[Transfer], receipt: dict, tx: dict,
                    payment_tokens: dict) -> Optional[Tuple[str, int]]:
    """Fallback for trades no marketplace event covers: what did the buyer pay in this transaction?"""
    bought_here = sum(1 for t in transfers if t.buyer == transfer.buyer)
    paid: Dict[str, int] = {}
    seller_paid = False
    for entry in receipt["logs"]:
        token = entry["address"].lower()
        topics = entry["topics"]
        if token in payment_tokens and len(topics) == 3 and topics[0] == TRANSFER_TOPIC:
            if topic_to_address(topics[1]) == transfer.buyer:
                paid[token] = paid.get(token, 0) + int(entry["data"], 16)
            if topic_to_address(topics[2]) == transfer.seller:
                seller_paid = True
    if paid:
        if not seller_paid:
            return None  # e.g. a loan repaid to a lender, with the NFT coming back out of the loan contract
        token, amount = next(iter(paid.items()))
        return token, amount // bought_here
    value = int(tx.get("value", "0x0"), 16)
    if value and tx["from"].lower() == transfer.buyer:
        return NATIVE, value // bought_here
    return None


def sales_in_transaction(contract: str, transfers: List[Transfer], receipt: dict, tx: dict, chain: Chain) -> List[Sale]:
    """Price each NFT transfer in one transaction. Transfers with no payment are not sales."""
    orders = [
        decode_order_fulfilled(entry) for entry in receipt["logs"]
        if entry["address"].lower() in SEAPORT_ADDRESSES and entry["topics"][0] == ORDER_FULFILLED_TOPIC
    ]
    to = (tx.get("to") or "").lower()
    blur = blur_prices(receipt)

    transfers = _merge_hops(transfers)
    sales = []
    for t in transfers:
        priced = seaport_price(orders, contract, t.token_id)
        if priced:
            marketplace = seaport_marketplace(orders, contract, t.token_id)
        elif blur.get((contract, t.token_id)):
            priced, marketplace = (NATIVE, blur[(contract, t.token_id)]), "Blur"
        else:
            priced = _direct_payment(t, transfers, receipt, tx, chain.payment_tokens)
            marketplace = chain.marketplaces.get(to)
        if to in SCATTER_ROUTERS and marketplace != "Scatter":
            # Bought or sold on Scatter's site, which also shows other marketplaces' listings and offers.
            marketplace = f"{marketplace} via Scatter" if marketplace else "Scatter"
        if not priced:
            continue
        sales.append(Sale(t.tx_hash, t.log_index, t.block, t.token_id, t.seller, t.buyer,
                          priced[0], priced[1], marketplace))
    return sales


def find_sales(rpc: RPC, contract: str, from_block: int, to_block: int, chain: Chain) -> List[Sale]:
    by_tx: "OrderedDict[str, List[Transfer]]" = OrderedDict()
    for t in nft_transfers(rpc.get_logs(contract, [TRANSFER_TOPIC], from_block, to_block)):
        by_tx.setdefault(t.tx_hash, []).append(t)
    sales = []
    for tx_hash, transfers in by_tx.items():
        sales += sales_in_transaction(contract, transfers, rpc.receipt(tx_hash), rpc.transaction(tx_hash), chain)
    return sales


def sales_for_tx(rpc: RPC, contract: str, tx_hash: str, chain: Chain) -> List[Sale]:
    """Used by `--tx`: re-run detection on one past transaction."""
    receipt = rpc.receipt(tx_hash)
    if receipt is None:
        raise SystemExit(f"Transaction {tx_hash} not found on {chain.name} (is CHAIN right?)")
    ours = [entry for entry in receipt["logs"] if entry["address"].lower() == contract]
    return sales_in_transaction(contract, nft_transfers(ours), receipt, rpc.transaction(tx_hash), chain)
