"""The printable QR sheet.

The QR encodes a plain HTTPS URL, never a payment QR: Razorpay QR codes only
work in live mode, so a test-mode payment QR is unscannable by any real UPI
app. A URL sidesteps that distinction entirely -- the customer's stock camera
opens it and the negotiation happens on the web.

The URL is upper-cased so segno picks QR alphanumeric mode instead of byte
mode. That keeps a 44-character URL at version 3 rather than 4, which is the
difference between scanning and not scanning at 35mm off paper under shop
lighting. HTTP URLs are case-insensitive in scheme and host, and our tokens
are already upper-case Crockford base32, so nothing is lost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import segno
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core import ids

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html"]),
)


def qr_svg(payload: str, scale: int = 4) -> str:
    """Inline SVG with no XML declaration and no namespace, for embedding.

    error='m' (15% recovery) is deliberate: printed-then-photographed codes
    lose modules to paper grain, glare and focus in a way screens never do.
    """
    qr = segno.make_qr(payload, error="m")
    # Colours are pinned rather than inherited. This sheet is a print artifact
    # that also gets viewed on screen, and a viewer in dark mode would
    # otherwise render dark modules on a dark ground -- unscannable, and worse,
    # it looks fine until someone actually tries to scan it.
    return qr.svg_inline(scale=scale, border=2, dark="#000000", light="#ffffff")


def render_sheet(campaign: dict[str, Any], slots: list[dict[str, Any]], base_url: str) -> str:
    tiles = []
    for slot in slots:
        url = ids.slot_url(base_url, slot["slot_token"])
        tiles.append(
            {
                "token": slot["slot_token"],
                "url": url,
                "ceiling_pct": f"{slot['ceiling_bps'] / 100:g}",
                "leaf_index": slot["leaf_index"],
                "status": slot["status"],
                "svg": qr_svg(url),
            }
        )
    return _env.get_template("qr_sheet.html").render(
        campaign=campaign,
        tiles=tiles,
        base_url=base_url,
        version=segno.make_qr(tiles[0]["url"]).version if tiles else None,
    )
