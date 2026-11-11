"""Builds each sale's post (text, price, image) and sends it to X."""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import string
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import requests
from PIL import Image
from requests_oauthlib import OAuth1

from chain import RPC, USER_AGENT, decode_string

log = logging.getLogger(__name__)


# ---- Text ----------------------------------------------------------------------------
# The layout is tweet_template.txt, one line of the post per line, read before every post.
# Don't add links: X charges $0.20 for a post with a URL vs $0.015 without.

TEMPLATE_FILE = Path(__file__).with_name("tweet_template.txt")
PLACEHOLDERS = {"emoji", "name", "token_id", "price", "symbol", "usd", "marketplace", "seller", "buyer"}


def read_template() -> List[str]:
    return [line for line in TEMPLATE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def template_problems(lines: List[str]) -> List[str]:
    """Human-readable errors, e.g. a misspelled {placeholder} or a stray brace. Such lines are left out of posts."""
    if not lines:
        return ["the template is empty"]
    problems = []
    for number, line in enumerate(lines, 1):
        try:
            names = {field for _, field, _, _ in string.Formatter().parse(line) if field is not None}
        except ValueError:
            problems.append(f"line {number}: unmatched {{ or }}")
            continue
        problems += [f"line {number}: unknown placeholder {{{name}}}" for name in sorted(names - PLACEHOLDERS)]
    return problems


def format_amount(value: Decimal) -> str:
    """1.0600 -> "1.06", 0.004213 -> "0.004213", 1234.5 -> "1,234.5"."""
    x = float(value)
    if x <= 0:
        return "0"
    if x >= 1:
        text = f"{x:,.2f}"
    else:
        decimals = max(2, 3 - math.floor(math.log10(x)))  # 4 significant digits
        text = f"{x:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def short_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


def format_tweet(
    *, emoji: str, name: str, token_id: int, price: Decimal, symbol: str,
    usd: Optional[Decimal], marketplace: Optional[str], seller: str, buyer: str,
    seller_name: Optional[str] = None, buyer_name: Optional[str] = None,
) -> str:
    fields = {
        "emoji": emoji,
        "name": name,
        "token_id": token_id,
        "price": format_amount(price),
        "symbol": symbol,
        "usd": f" (${usd:,.2f})" if usd is not None else "",
        "marketplace": marketplace,
        "seller": seller_name or short_address(seller),
        "buyer": buyer_name or short_address(buyer),
    }
    present = {k: v for k, v in fields.items() if v is not None}
    out = []
    for line in read_template():
        try:
            out.append(line.format(**present))
        except (KeyError, ValueError, IndexError):
            continue  # a value this line needs is missing (e.g. no marketplace), or the line has a typo
    return "\n".join(out)


# ---- Prices --------------------------------------------------------------------------

COINBASE_URL = "https://api.coinbase.com/v2/prices/{}-USD/spot"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=usd"
COINGECKO_IDS = {"MNT": "mantle"}  # coins Coinbase lists under another meaning of the same ticker


class Pricer:
    """Turns raw payment amounts into human prices and USD values."""

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


# ---- Images --------------------------------------------------------------------------

FALLBACK_GATEWAYS = ["https://ipfs.filebase.io/ipfs/", "https://gateway.pinata.cloud/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]
MAX_DOWNLOAD = 30 * 1024 * 1024
X_IMAGE_LIMIT = 5 * 1024 * 1024
MIN_SIDE = 1000  # small (pixel-art) images get scaled up so they don't look blurry on X


def _gateway_urls(uri: str, gateway: str) -> list:
    if uri.startswith("ipfs://"):
        path = uri[len("ipfs://"):]
        if path.startswith("ipfs/"):
            path = path[len("ipfs/"):]
        return [g + path for g in dict.fromkeys([gateway] + FALLBACK_GATEWAYS)]
    if uri.startswith("ar://"):
        return ["https://arweave.net/" + uri[len("ar://"):]]
    return [uri]


def fetch_uri(uri: str, gateway: str) -> bytes:
    if uri.startswith("data:"):
        header, _, payload = uri.partition(",")
        return base64.b64decode(payload) if header.endswith(";base64") else unquote(payload).encode()
    last_error: Exception = ValueError(f"unsupported URI {uri}")
    for url in _gateway_urls(uri, gateway):
        try:
            resp = requests.get(url, timeout=30, stream=True, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.raw.read(MAX_DOWNLOAD + 1, decode_content=True)
            if len(data) > MAX_DOWNLOAD:
                raise ValueError("file too large")
            return data
        except Exception as e:
            last_error = e
            log.debug("Fetching %s failed: %s", url, e)
    raise last_error


def image_bytes(rpc: RPC, contract: str, token_id: int, gateway: str, url_template: str) -> bytes:
    if url_template:
        return fetch_uri(url_template.format(id=token_id), gateway)
    token_uri = decode_string(rpc.eth_call(contract, "0xc87b56dd" + format(token_id, "064x")))  # tokenURI(id)
    metadata = json.loads(fetch_uri(token_uri, gateway))
    image = metadata.get("image") or metadata.get("image_url")
    if image:
        return fetch_uri(image, gateway)
    if metadata.get("image_data"):  # raw SVG stored in the metadata
        return metadata["image_data"].encode()
    raise ValueError(f"metadata at {token_uri} has no image")


def prepare_for_x(data: bytes) -> Tuple[bytes, str]:
    """Convert to a PNG/JPEG under X's 5 MB image limit. Animated GIFs become their first frame."""
    if b"<svg" in data[:2048].lower():
        try:
            import cairosvg  # optional; needs the Cairo library installed (brew install cairo)
        except Exception:
            raise ValueError("image is an SVG; `pip install cairosvg` to convert it for X")
        data = cairosvg.svg2png(bytestring=data, output_width=1200)
    img = Image.open(io.BytesIO(data))
    if img.format in ("PNG", "JPEG") and len(data) <= X_IMAGE_LIMIT and max(img.size) >= MIN_SIDE:
        return data, "image/png" if img.format == "PNG" else "image/jpeg"

    img.seek(0)
    img = img.convert("RGBA")
    if max(img.size) < MIN_SIDE:
        factor = math.ceil(MIN_SIDE / max(img.size))
        img = img.resize((img.width * factor, img.height * factor), Image.NEAREST)
    img.thumbnail((4096, 4096), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    if out.tell() <= X_IMAGE_LIMIT:
        return out.getvalue(), "image/png"
    for quality in (92, 85, 75, 60):
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=quality)
        if out.tell() <= X_IMAGE_LIMIT:
            return out.getvalue(), "image/jpeg"
    raise ValueError("couldn't get the image under 5 MB")


def get_image(rpc: RPC, contract: str, token_id: int, gateway: str, url_template: str) -> Optional[Tuple[bytes, str]]:
    """Returns (bytes, mime type), or None if the image couldn't be loaded — the post goes out without it."""
    try:
        return prepare_for_x(image_bytes(rpc, contract, token_id, gateway, url_template))
    except Exception as e:
        log.warning("No image for #%s: %s", token_id, e)
        return None


# ---- X -------------------------------------------------------------------------------
# The X API v2, signed with OAuth 1.0a user keys.

X_API = "https://api.x.com/2"


class XError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"X API error {status}: {body}")
        self.status = status


class XClient:
    def __init__(self, api_key: str, api_secret: str, access_token: str, access_token_secret: str):
        self.session = requests.Session()
        self.session.auth = OAuth1(api_key, api_secret, access_token, access_token_secret)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(3):
            resp = self.session.request(method, X_API + path, timeout=60, **kwargs)
            if resp.status_code == 429:
                reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 60))
                wait = min(max(reset - time.time(), 5), 900)
                log.warning("X rate limit hit, waiting %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            if not resp.ok:
                raise XError(resp.status_code, resp.text[:500])
            return resp.json()
        raise XError(429, "still rate limited after retries")

    def me(self) -> str:
        return self._request("GET", "/users/me")["data"]["username"]

    def upload_image(self, data: bytes, mime: str) -> str:
        ext = "png" if mime == "image/png" else "jpg"
        result = self._request(
            "POST", "/media/upload",
            files={"media": (f"nft.{ext}", data, mime)},
            data={"media_category": "tweet_image"},
        )
        return result["data"]["id"]

    def post(self, text: str, media_id: Optional[str] = None) -> str:
        body: dict = {"text": text}
        if media_id:
            body["media"] = {"media_ids": [media_id]}
        return self._request("POST", "/tweets", json=body)["data"]["id"]
