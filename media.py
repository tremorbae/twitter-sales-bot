"""Finds an NFT's image (tokenURI -> metadata -> image) and makes it X-friendly."""
from __future__ import annotations

import base64
import io
import json
import logging
import math
from typing import Optional, Tuple
from urllib.parse import unquote

import requests
from PIL import Image

from chain import RPC, decode_string, encode_uint

log = logging.getLogger(__name__)

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


def _decode_data_uri(uri: str) -> bytes:
    header, _, payload = uri.partition(",")
    return base64.b64decode(payload) if header.endswith(";base64") else unquote(payload).encode()


def fetch_uri(uri: str, gateway: str) -> bytes:
    if uri.startswith("data:"):
        return _decode_data_uri(uri)
    last_error: Exception = ValueError(f"unsupported URI {uri}")
    for url in _gateway_urls(uri, gateway):
        try:
            resp = requests.get(url, timeout=30, stream=True, headers={"User-Agent": "nft-sales-bot/1.0"})
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
    token_uri = decode_string(rpc.eth_call(contract, "0xc87b56dd" + encode_uint(token_id)))
    metadata = json.loads(fetch_uri(token_uri, gateway))
    image = metadata.get("image") or metadata.get("image_url")
    if image:
        return fetch_uri(image, gateway)
    if metadata.get("image_data"):  # raw SVG stored in the metadata
        return metadata["image_data"].encode()
    raise ValueError(f"metadata at {token_uri} has no image")


def _svg_to_png(data: bytes) -> bytes:
    try:
        import cairosvg  # optional; needs the Cairo library installed (brew install cairo)
    except Exception:
        raise ValueError("image is an SVG; `pip install cairosvg` to convert it for X")
    return cairosvg.svg2png(bytestring=data, output_width=1200)


def prepare_for_x(data: bytes) -> Tuple[bytes, str]:
    """Convert to a PNG/JPEG under X's 5 MB image limit. Animated GIFs become their first frame."""
    if b"<svg" in data[:2048].lower():
        data = _svg_to_png(data)
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
