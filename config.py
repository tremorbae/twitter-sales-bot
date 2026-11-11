"""Settings for the sales bot. Values come from the .env file (see the README)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

NATIVE = "0x0000000000000000000000000000000000000000"  # stands in for the chain's own coin (ETH, BERA, ...)

# ---- Marketplaces -------------------------------------------------------------------
# Seaport is the exchange contract OpenSea (and Scatter) settle trades through.
SEAPORT_ADDRESSES = {
    "0x0000000000000068f116a894984e2db1123eb395",  # Seaport 1.6
    "0x00000000006687982678b03100b9bdc8be440814",  # Seaport 1.6, OpenSea's alternate deployment (MegaETH)
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc",  # Seaport 1.5
    "0x00000000006c3852cbef3e08e8df289169ede581",  # Seaport 1.1
}
# An order is OpenSea's if it pays OpenSea's fee or uses OpenSea's signed zone (from OpenSea's SDK).
OPENSEA_FEE_RECIPIENTS = {
    "0x0000a26b00c1f0df003000390027140000faa719",  # most chains
    "0x07d3a100c3880830dd43fe5c938b5144721ce9d6",  # MegaETH, Ink
}
OPENSEA_ZONES = {"0x000056f7000000ece9003ca63978907a00ffd100", "0xdfe0000000005ce3008800300037e4c803ed08c7"}

BLUR_EXCHANGE = "0xb2ecfe4e4d61f8790bbb9de2d1259b9e2410cea5"  # Blur's marketplace (Ethereum only)

# Scatter (scatter.art) settles trades through Seaport. These are the current contracts and older versions,
# from scatter.art's own site code; the same addresses on every chain it supports except MegaETH's own set.
SCATTER_ROUTERS = {  # a buyer's or seller's transaction is sent to one of these
    "0xfa2c892e62b0e3df63c10029a1d0d0b17ae4e651", "0x171d543f8246479214440acdd642abb9fce7d6f8",
    "0xc93803e782df6b94d757623b3116a969ce5fb936", "0xa629f2041dfde5164f0808849e6898b72556e620",
    "0x9eefc0099206ae8d82098fcfda41bbb214e8e6ab", "0xc4e79e1930b79d0a4b163884a3a22702f87f013b",  # offer routers
    "0xea494c988948e81bc4d6a6b1b04b967fc218c5ef", "0x4b6d1d313b8d6103efbd8b8dbed1c625651cbac6",
    "0x201a14fb3bb8199c3e6b89eb90c27decfcf77e7a", "0x2871b357db5105776e667770c636919ac2aec53c",  # MegaETH
}
SCATTER_OFFERERS = {  # Seaport orders for listings and offers made on Scatter
    "0x7f5ffab083856c7d3b2f560607272c7a53921bbb", "0x5b67af68cd4f2497d756d7c6baa8f3dbbe6a3a85",
    "0xa84b92b693581cf63b923971fab138a9098a7ca9", "0x0cb5ceb2dbe1d42ae9791b1cdb4f11fde9e71675",
    "0x93fb4375ed8aff95f1070aee8df95549530c750e",  # MegaETH
}


# ---- Chains -------------------------------------------------------------------------

def coin(symbol: str, decimals: int = 18) -> dict:
    """A token worth one `symbol` each, e.g. WETH is coin("ETH")."""
    return {"symbol": symbol, "decimals": decimals, "peg": symbol}


def usd(symbol: str, decimals: int = 6) -> dict:
    """A dollar stablecoin."""
    return {"symbol": symbol, "decimals": decimals, "peg": "USD"}


@dataclass
class Chain:
    name: str
    chain_id: int
    rpc_url: str
    # Blocks to wait before reading a block, in case the chain replaces it (a reorg). About 2-15 seconds.
    confirmations: int
    native: str = "ETH"  # the chain's own coin
    # ERC-20 tokens buyers pay with, besides the native coin: wrapped native coin, WETH, stablecoins.
    tokens: dict = field(default_factory=dict)
    # Other marketplace contracts, by the address a buyer's transaction is sent to.
    marketplaces: dict = field(default_factory=dict)

    @property
    def payment_tokens(self) -> dict:
        return {NATIVE: coin(self.native), **self.tokens}


WETH_OP = "0x4200000000000000000000000000000000000006"  # WETH on OP Stack chains (Base, Optimism, Zora...)

# Every RPC below is free, needs no key, and was tested: right chain, recent logs, and (except Monad,
# about a day) a month of history so the bot can catch up after downtime. Token addresses come from
# OpenSea's SDK and were checked on-chain.
CHAINS = {
    "ethereum": Chain("Ethereum", 1, "https://mainnet.gateway.tenderly.co", 2, tokens={
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": coin("ETH"),  # WETH
        "0x0000000000a39bb272e79075ade125fd351887ac": coin("ETH"),  # Blur Pool (Blur bids)
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": usd("USDC"),
    }, marketplaces={
        BLUR_EXCHANGE: "Blur",
        "0xdf288b66a77f25197544877eaf4626d7e64cb5a8": "NFTX",  # NFTXZap: buys from NFTX pools
    }),
    "base": Chain("Base", 8453, "https://base.gateway.tenderly.co", 25, tokens={
        WETH_OP: coin("ETH"), "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": usd("USDC")}),
    "arbitrum": Chain("Arbitrum", 42161, "https://arb1.arbitrum.io/rpc", 20, tokens={
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": coin("ETH"), "0xaf88d065e77c8cc2239327c5edb3a432268e5831": usd("USDC")}),
    "optimism": Chain("Optimism", 10, "https://optimism.gateway.tenderly.co", 3, tokens={
        WETH_OP: coin("ETH"), "0x0b2c639c533813f4aa9d7837caf62653d097ff85": usd("USDC")}),
    "polygon": Chain("Polygon", 137, "https://polygon-bor-rpc.publicnode.com", 10, native="POL", tokens={
        "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": coin("ETH"),  # WETH: OpenSea's listing and offer currency here
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": coin("POL"),  # WPOL
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": usd("USDC"),
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": usd("USDT")}),
    "abstract": Chain("Abstract", 2741, "https://api.mainnet.abs.xyz", 2, tokens={
        "0x3439153eb7af838ad19d56e1571fbd09333c2809": coin("ETH"), "0x84a71ccd554cc1b02749b35d22f684cc8ec987e1": usd("USDC")}),
    "apechain": Chain("ApeChain", 33139, "https://rpc.apechain.com", 5, native="APE", tokens={
        "0x48b62137edfa95a428d35c09e44256a739f6b557": coin("APE")}),
    "berachain": Chain("Berachain", 80094, "https://rpc.berachain.com", 2, native="BERA", tokens={
        "0x6969696969696969696969696969696969696969": coin("BERA"), "0x549943e04f40284185054145c6e4e9568c1d3241": usd("USDC")}),
    "megaeth": Chain("MegaETH", 4326, "https://mainnet.megaeth.com/rpc", 5, tokens={WETH_OP: coin("ETH")}),
    "monad": Chain("Monad", 143, "https://monad.gateway.tenderly.co", 10, native="MON", tokens={
        "0x3bd359c1119da7da1d913d1c4d2b7c461115433a": coin("MON"), "0x754704bc059f8c67012fed69bc8a327a5aafb603": usd("USDC")}),
    "hyperevm": Chain("HyperEVM", 999, "https://rpc.hypurrscan.io", 5, native="HYPE", tokens={
        "0x5555555555555555555555555555555555555555": coin("HYPE"), "0xb88339cb7199b77e23db6e890353e22632ba630f": usd("USDC")}),
    "ink": Chain("Ink", 57073, "https://rpc-qnd.inkonchain.com", 5, tokens={
        WETH_OP: coin("ETH"), "0x2d270e6886d130d724215a266106e6832161eaed": usd("USDC")}),
    "mantle": Chain("Mantle", 5000, "https://mantle.gateway.tenderly.co", 3, native="MNT", tokens={
        "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8": coin("MNT"), "0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111": coin("ETH"),
        "0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9": usd("USDC")}),
    "zora": Chain("Zora", 7777777, "https://rpc.zora.energy", 3, tokens={WETH_OP: coin("ETH")}),
    "shape": Chain("Shape", 360, "https://mainnet.shape.network", 3, tokens={WETH_OP: coin("ETH")}),
    "unichain": Chain("Unichain", 130, "https://unichain.gateway.tenderly.co", 5, tokens={
        WETH_OP: coin("ETH"), "0x078d782b760474a361dda0af3839290b0ef57ad6": usd("USDC")}),
    "soneium": Chain("Soneium", 1868, "https://soneium.gateway.tenderly.co", 3, tokens={
        WETH_OP: coin("ETH"), "0xba9986d2381edf1da03b0b9c1f8b00dc4aacc369": usd("USDC")}),
    "avalanche": Chain("Avalanche", 43114, "https://avalanche-c-chain-rpc.publicnode.com", 3, native="AVAX", tokens={
        "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7": coin("AVAX"),  # WAVAX: OpenSea's listing and offer currency here
        "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab": coin("ETH"),  # WETH.e
        "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": usd("USDC")}),
    "sei": Chain("Sei", 1329, "https://evm-rpc.sei-apis.com", 5, native="SEI", tokens={
        "0xe30fedd158a2e3b13e9badaeabafc5516e95e8c7": coin("SEI"), "0xe15fc38f6d8c56af07bbcbe3baf5708a2bf42392": usd("USDC")}),
    "robinhood": Chain("Robinhood Chain", 4663, "https://rpc.mainnet.chain.robinhood.com", 20, tokens={
        "0x0bd7d308f8e1639fab988df18a8011f41eacad73": coin("ETH"), "0x5fc5360d0400a0fd4f2af552add042d716f1d168": usd("USDG")}),
}


def find_chain(value: str) -> Optional[Chain]:
    """A built-in chain by name ("base") or chain ID ("8453")."""
    value = value.strip().lower()
    return CHAINS.get(value) or next((c for c in CHAINS.values() if str(c.chain_id) == value), None)


# ---- .env settings ---------------------------------------------------------------

def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: Optional[int]) -> Optional[int]:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


@dataclass
class Settings:
    chain: Chain
    contract: str
    collection_name: str  # empty: use the name the contract reports
    emoji: str
    rpc_url: str
    poll_seconds: int
    confirmations: int
    max_block_range: int
    start_block: Optional[int]
    min_price_usd: float
    seconds_between_posts: float
    dry_run: bool
    ipfs_gateway: str
    ens_rpc_url: str
    image_url_template: str
    state_file: str
    x_api_key: str
    x_api_secret: str
    x_access_token: str
    x_access_token_secret: str

    @property
    def has_x_keys(self) -> bool:
        return all([self.x_api_key, self.x_api_secret, self.x_access_token, self.x_access_token_secret])


def load_settings() -> Settings:
    chain_value = os.getenv("CHAIN", "").strip()
    rpc_url = os.getenv("RPC_URL", "").strip()
    chain = find_chain(chain_value) if chain_value else None
    if chain is None:
        if not chain_value.isdigit():
            raise SystemExit(f"Set CHAIN to the chain your collection is on: {', '.join(CHAINS)}, "
                             f"or another EVM chain's ID number")
        if not rpc_url:
            raise SystemExit(f"Chain {chain_value} isn't built in: also set RPC_URL to an RPC for it")
        # Any other EVM chain: OpenSea and Scatter sales are still found; set NATIVE_SYMBOL for USD prices.
        chain = Chain(f"chain {chain_value}", int(chain_value), rpc_url, 10,
                      native=os.getenv("NATIVE_SYMBOL", "").strip().upper() or "ETH")

    contract = os.getenv("CONTRACT_ADDRESS", "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", contract):
        raise SystemExit("Set CONTRACT_ADDRESS to your collection's 0x... contract address")

    gateway = os.getenv("IPFS_GATEWAY", "").strip() or "https://ipfs.filebase.io/ipfs/"
    if not gateway.endswith("/"):
        gateway += "/"

    return Settings(
        chain=chain,
        contract=contract,
        collection_name=os.getenv("COLLECTION_NAME", "").strip(),
        emoji=os.getenv("EMOJI", "").strip() or "🖼️",
        rpc_url=rpc_url or chain.rpc_url,
        poll_seconds=_int("POLL_SECONDS", 15),
        confirmations=_int("CONFIRMATIONS", chain.confirmations),
        max_block_range=_int("MAX_BLOCK_RANGE", 5000),
        start_block=_int("START_BLOCK", None),
        min_price_usd=_float("MIN_PRICE_USD", 0),
        seconds_between_posts=_float("SECONDS_BETWEEN_POSTS", 5),
        # Defaults to a dry run so nothing is posted until you opt in.
        dry_run=_bool("DRY_RUN", True),
        ipfs_gateway=gateway,
        # Ethereum mainnet RPC for ENS names; set ENS_RPC_URL= "off" to show plain addresses.
        ens_rpc_url=os.getenv("ENS_RPC_URL", "").strip() or "https://ethereum-rpc.publicnode.com",
        image_url_template=os.getenv("IMAGE_URL_TEMPLATE", "").strip(),
        state_file=os.getenv("STATE_FILE", "").strip() or "state.json",
        x_api_key=os.getenv("X_API_KEY", "").strip(),
        x_api_secret=os.getenv("X_API_SECRET", "").strip(),
        x_access_token=os.getenv("X_ACCESS_TOKEN", "").strip(),
        x_access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET", "").strip(),
    )
