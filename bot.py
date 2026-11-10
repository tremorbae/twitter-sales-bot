"""Watches the chain for sales of your collection and posts each one to X.

    python bot.py               run forever (dry run unless DRY_RUN=false in .env)
    python bot.py --check       test the RPC, contract and X keys
    python bot.py --recent      show the posts for the collection's last 3 sales (never posts)
    python bot.py --tx 0x...    show the post for one past sale transaction
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import warnings
from pathlib import Path
from typing import List, Optional

# Harmless notice from macOS's built-in Python; hide it so real errors stand out.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from chain import RPC, RPCError, Sale, decode_string, find_sales, sales_for_tx
from config import Settings, load_settings
from ens import ENS
from media import get_image
from prices import Pricer
from tweet import check_template, format_tweet
from x_client import XClient, XError

log = logging.getLogger("salesbot")

OUT_DIR = Path("out")  # where dry runs save images
NAME_CALL = "0x06fdde03"  # name()
SUPPORTS_INTERFACE = "0x01ffc9a7"  # supportsInterface(bytes4)
ERC721, ERC1155 = "80ac58cd", "d9b67a26"
RECENT_MAX_SCANS = 30  # --recent looks back up to ~8M blocks: years on Ethereum, ~9 days on Robinhood Chain


class State:
    """The last block scanned and recently posted sales, so a restart never double-posts."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.last_block: Optional[int] = None
        self.posted: list = []
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.last_block = data.get("last_block")
            self.posted = data.get("posted", [])

    def seen(self, sale_id: str) -> bool:
        return sale_id in self.posted

    def mark(self, sale_id: str) -> None:
        self.posted = (self.posted + [sale_id])[-2000:]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_block": self.last_block, "posted": self.posted}))
        os.replace(tmp, self.path)


class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.rpc = RPC(settings.rpc_url)
        self.pricer = Pricer(self.rpc, settings.chain.payment_tokens)
        self.ens = ENS(settings.ens_rpc_url) if settings.ens_rpc_url != "off" else None
        self.x: Optional[XClient] = None
        if settings.has_x_keys:
            self.x = XClient(settings.x_api_key, settings.x_api_secret,
                             settings.x_access_token, settings.x_access_token_secret)
        elif not settings.dry_run:
            raise SystemExit("DRY_RUN is false but the X_API_KEY / X_ACCESS_TOKEN values in .env are not all set")
        self._last_post = 0.0

    def collection_name(self) -> str:
        """COLLECTION_NAME, or else the name the contract reports."""
        if not self.s.collection_name:
            try:
                self.s.collection_name = decode_string(self.rpc.eth_call(self.s.contract, NAME_CALL)).strip()
            except (RPCError, ValueError):
                pass
            if not self.s.collection_name:
                raise SystemExit("The contract doesn't report a name: set COLLECTION_NAME in .env")
        return self.s.collection_name

    def announce(self, sale: Sale) -> None:
        price, symbol, usd = self.pricer.describe(sale.payment_token, sale.amount)
        if self.s.min_price_usd and usd is not None and usd < self.s.min_price_usd:
            log.info("Skipping #%s at $%.2f (under MIN_PRICE_USD)", sale.token_id, usd)
            return

        text = format_tweet(
            emoji=self.s.emoji, name=self.collection_name(), token_id=sale.token_id,
            price=price, symbol=symbol, usd=usd, marketplace=sale.marketplace,
            seller=sale.seller, buyer=sale.buyer,
            seller_name=self.ens.name(sale.seller) if self.ens else None,
            buyer_name=self.ens.name(sale.buyer) if self.ens else None,
        )
        image = get_image(self.rpc, self.s.contract, sale.token_id, self.s.ipfs_gateway, self.s.image_url_template)

        if self.s.dry_run:
            log.info("[dry run] would post:\n%s", text)
            if image:
                OUT_DIR.mkdir(exist_ok=True)
                path = OUT_DIR / f"{sale.token_id}-{sale.tx_hash[:10]}.{'png' if image[1] == 'image/png' else 'jpg'}"
                path.write_bytes(image[0])
                log.info("[dry run] image saved to %s", path)
            return

        wait = self.s.seconds_between_posts - (time.time() - self._last_post)
        if wait > 0:
            time.sleep(wait)
        media_id = None
        if image:
            try:
                media_id = self.x.upload_image(*image)
            except XError as e:
                log.warning("Image upload failed, posting without the image: %s", e)
        tweet_id = self.x.post(text, media_id)
        self._last_post = time.time()
        log.info("Posted #%s: https://x.com/i/status/%s", sale.token_id, tweet_id)

    def handle(self, sale: Sale, state: State) -> None:
        if state.seen(sale.id):
            return
        try:
            self.announce(sale)
        except XError as e:
            # X refuses identical posts; that means this one already went out.
            if not (e.status == 403 and "duplicate" in str(e).lower()):
                raise
            log.warning("X says #%s was already posted; skipping", sale.token_id)
        state.mark(sale.id)
        state.save()

    def catch_up(self, state: State) -> None:
        head = self.rpc.block_number() - self.s.confirmations
        while state.last_block < head:
            start = state.last_block + 1
            end = min(head, start + self.s.max_block_range - 1)
            for sale in find_sales(self.rpc, self.s.contract, start, end, self.s.chain):
                self.handle(sale, state)
            state.last_block = end
            state.save()

    def run(self) -> None:
        state = State(self.s.state_file)
        if state.last_block is None:
            head = self.rpc.block_number() - self.s.confirmations
            state.last_block = self.s.start_block - 1 if self.s.start_block else head
            state.save()
        log.info("Watching %s (%s) on %s from block %d%s", self.collection_name(), self.s.contract,
                 self.s.chain.name, state.last_block + 1, " (DRY RUN: nothing will be posted)" if self.s.dry_run else "")
        errors = 0
        while True:
            try:
                self.catch_up(state)
                errors = 0
            except Exception as e:
                # Nothing is skipped: the scan resumes from the last finished block.
                errors += 1
                log.error("Error, will retry: %s", e, exc_info=errors == 1)
            time.sleep(self.s.poll_seconds * min(2 ** errors, 20))

    def recent(self, count: int) -> None:
        """Dry-run the collection's last `count` sales, searching back from the newest block."""
        log.info("Looking for the last %d sales of %s on %s...", count, self.collection_name(), self.s.chain.name)
        newest = end = self.rpc.block_number() - self.s.confirmations
        span = self.s.max_block_range
        sales: List[Sale] = []
        for _ in range(RECENT_MAX_SCANS):
            start = max(0, end - span + 1)
            sales = find_sales(self.rpc, self.s.contract, start, end, self.s.chain) + sales
            if len(sales) >= count or start == 0:
                break
            # Look further back with each step, so quiet collections and fast chains need few requests.
            end, span = start - 1, min(span * 2, self.s.max_block_range * 64)
        if not sales:
            log.info("No sales found in blocks %s-%s.", f"{start:,}", f"{newest:,}")
        for sale in sales[-count:]:
            self.announce(sale)

    def _supports(self, interface_id: str) -> bool:
        try:
            return int(self.rpc.eth_call(self.s.contract, SUPPORTS_INTERFACE + interface_id + "0" * 56), 16) == 1
        except (RPCError, ValueError):
            return False

    def check(self) -> None:
        chain = self.s.chain
        chain_id = self.rpc.chain_id()
        if chain_id == chain.chain_id:
            print(f"RPC: connected to {chain.name} (chain id {chain_id})")
        else:
            print(f"RPC: connected, but to chain id {chain_id}, not {chain.name} ({chain.chain_id}). Check CHAIN and RPC_URL.")
        try:
            print(f"Contract: {decode_string(self.rpc.eth_call(self.s.contract, NAME_CALL))}")
        except Exception as e:
            print(f"Contract: couldn't read name() ({e}) - is CONTRACT_ADDRESS right?")
        if self._supports(ERC1155) and not self._supports(ERC721):
            print("  WARNING: this is an ERC-1155 (editions) contract. The bot only posts ERC-721 sales.")
        problems = check_template()
        if problems:
            print("Post template: posts use the default layout until these are fixed in tweet_template.txt:")
            for problem in problems:
                print(f"  {problem}")
        else:
            print("Post template: OK")
        coin = chain.native
        rate = self.pricer.usd_price(coin)
        print(f"{coin} price: ${rate:,.4g}" if rate else f"{coin} price: unavailable (posts show no USD value)")
        if self.x:
            print(f"X: authenticated as @{self.x.me()}")
        else:
            print("X: keys not set (fine for a dry run)")
        print(f"Mode: {'DRY RUN' if self.s.dry_run else 'LIVE - will post to X'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="NFT sales bot for X (Robinhood Chain and Ethereum)")
    parser.add_argument("--check", action="store_true", help="test the RPC, contract and X keys, then exit")
    parser.add_argument("--recent", nargs="?", const=3, type=int, metavar="N",
                        help="show the posts for the collection's last N sales (default 3) without posting, then exit")
    parser.add_argument("--tx", metavar="HASH", help="process one past transaction, then exit")
    parser.add_argument("--dry-run", action="store_true", help="never post, even if DRY_RUN=false")
    args = parser.parse_args()
    if args.recent is not None and args.recent < 1:
        parser.error("--recent takes a number of sales, 1 or more")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    if args.dry_run or args.recent is not None:
        settings.dry_run = True
    bot = Bot(settings)

    if args.check:
        bot.check()
    elif args.recent is not None:
        bot.recent(args.recent)
    elif args.tx:
        sales = sales_for_tx(bot.rpc, settings.contract, args.tx.lower(), settings.chain)
        if not sales:
            print("No sale of this collection found in that transaction.")
        for sale in sales:
            bot.announce(sale)
    else:
        bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
