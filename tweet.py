"""The text of each sale post.

The format lives in tweet_template.txt (one line of the post per line). The bot
re-reads it before every post, so edits apply without a restart.
"""
from __future__ import annotations

import math
import string
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

TEMPLATE_FILE = Path(__file__).with_name("tweet_template.txt")

# Used when tweet_template.txt doesn't exist.
# Don't add links: X charges $0.20 for a post with a URL vs $0.015 without.
DEFAULT_LINES = [
    "{emoji} {name} #{token_id} Bought",
    "💰 {price} {symbol}{usd}",
    "🛒 {marketplace}",
    "👤 {seller} → {buyer}",
]

FIELDS = {
    "emoji": "collection emoji (EMOJI in .env)",
    "name": "collection name (COLLECTION_NAME in .env)",
    "token_id": "token number, e.g. 2863",
    "price": "price, e.g. 0.003849",
    "symbol": "the coin paid, e.g. ETH, APE or USDC",
    "usd": 'USD value with its brackets, e.g. " ($9.87)"',
    "marketplace": "e.g. OpenSea (line is dropped if unknown)",
    "seller": "seller's ENS name, or their wallet shortened",
    "buyer": "buyer's ENS name, or their wallet shortened",
}


def _template_lines() -> Optional[List[str]]:
    if not TEMPLATE_FILE.exists():
        return None
    return [line for line in TEMPLATE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_template() -> List[str]:
    lines = _template_lines()
    if lines and not template_problems(lines):
        return lines
    return list(DEFAULT_LINES)


def check_template() -> List[str]:
    """Problems with tweet_template.txt that make posts fall back to the default layout."""
    lines = _template_lines()
    return [] if lines is None else template_problems(lines)


def template_problems(lines: List[str]) -> List[str]:
    """Human-readable errors, e.g. a misspelled {placeholder} or a stray brace."""
    problems = []
    for number, line in enumerate(lines, 1):
        try:
            names = [field for _, field, _, _ in string.Formatter().parse(line) if field is not None]
        except ValueError:
            problems.append(f"Line {number}: unmatched {{ or }}")
            continue
        for name in names:
            if name not in FIELDS:
                problems.append(f"Line {number}: unknown placeholder {{{name}}}")
    if not any(line.strip() for line in lines):
        problems.append("The template is empty")
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
    lines: Optional[List[str]] = None,
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
    for line in lines if lines is not None else load_template():
        try:
            out.append(line.format(**present))
        except (KeyError, ValueError, IndexError):
            continue  # a value this line needs is missing
    return "\n".join(out)

