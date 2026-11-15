"""
Autocrat — one-tap compose links for human-assisted sends.

First-touch messages are never sent by automation: Autocrat stages a draft and
hands a human a link that opens the right app with the text pre-filled.

  * Desk (Windows): WhatsApp Web, because Coexistence numbers can't link the
    native WhatsApp for Windows app as a companion device.
  * Phone (Telegram alert button): wa.me, which opens the WhatsApp app.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode


def _digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        raise ValueError(f"Not a phone number: {phone!r}")
    return digits


def whatsapp_web_url(phone: str, text: str) -> str:
    """Desk [A] action: opens WhatsApp Web with the draft pre-filled."""
    return f"https://web.whatsapp.com/send?{urlencode({'phone': _digits(phone), 'text': text}, quote_via=quote)}"


def wa_me_url(phone: str, text: str) -> str:
    """Phone button: opens the WhatsApp app with the draft pre-filled."""
    return f"https://wa.me/{_digits(phone)}?{urlencode({'text': text}, quote_via=quote)}"


def x_dm_url(recipient_id: str, text: str) -> str:
    return f"https://x.com/messages/compose?{urlencode({'recipient_id': recipient_id, 'text': text}, quote_via=quote)}"


def reddit_message_url(username: str, subject: str, text: str) -> str:
    user = re.sub(r"^/?u/", "", username or "")
    params = urlencode({"to": user, "subject": subject, "message": text}, quote_via=quote)
    return f"https://www.reddit.com/message/compose/?{params}"


def compose_links(channel: str, contact: str, text: str, subject: str = "") -> dict:
    """All one-tap links for a staged draft, keyed by where they're used."""
    channel = (channel or "").lower()
    if channel in ("whatsapp", "wa"):
        return {"desk": whatsapp_web_url(contact, text), "phone": wa_me_url(contact, text)}
    if channel in ("x", "twitter"):
        url = x_dm_url(contact, text)
        return {"desk": url, "phone": url}
    if channel == "reddit":
        url = reddit_message_url(contact, subject or "Quick idea", text)
        return {"desk": url, "phone": url}
    return {}
