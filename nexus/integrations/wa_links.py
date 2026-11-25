"""
wa.me links for messages YOU send by hand (no automation, no LLM, zero tokens).

Input (what you paste to the bot after /wa):
    Hi {name}, saw your listings on Bayut, quick one: who answers new leads after 9pm?
    Omar Khan, 050 123 4567
    +971 55 765 4321
    Sara - 971501112233

Lines holding a phone number are contacts (name = the rest of the line); every other line is the
message. {name} becomes the contact's first name, or disappears cleanly when there is none.
Numbers without a country code get WA_LINKS_DEFAULT_CC (971). Duplicates and do-not-contact
numbers are skipped.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def normalize(raw: str, default_cc: str = "971") -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    elif (raw or "").strip().startswith("+"):
        pass
    elif digits.startswith("0"):
        digits = default_cc + digits[1:]
    elif len(digits) <= 9:
        digits = default_cc + digits
    return digits if 8 <= len(digits) <= 15 else ""


def parse(text: str, default_cc: str = "971") -> Tuple[str, List[Dict[str, str]]]:
    message_lines: List[str] = []
    contacts: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        m = PHONE_RE.search(line)
        if m and len(re.sub(r"\D", "", m.group(0))) >= 8:
            number = normalize(m.group(0), default_cc)
            name = (line[:m.start()] + " " + line[m.end():]).strip(" ,;:-|\t")
            if number:
                contacts.append({"number": number, "name": re.sub(r"\s+", " ", name)})
        else:
            message_lines.append(line)
    return "\n".join(message_lines).strip(), contacts


def personalize(template: str, name: str) -> str:
    first = (name or "").split()[0].strip(".,").title() if (name or "").strip() else ""
    if first:
        return template.replace("{name}", first)
    out = re.sub(r"\s*,?\s*\{name\}", "", template)          # "Hi {name}, x" → "Hi, x"
    return re.sub(r"\s+([,.!?])", r"\1", out).strip()


def build(text: str, default_cc: str = "971", is_blocked=None) -> Dict[str, Any]:
    template, contacts = parse(text, default_cc)
    seen, links, skipped = set(), [], []
    for c in contacts:
        if c["number"] in seen:
            skipped.append((c, "duplicate"))
            continue
        seen.add(c["number"])
        if is_blocked and is_blocked(c["number"]):
            skipped.append((c, "do-not-contact"))
            continue
        msg = personalize(template, c["name"])
        links.append({"name": c["name"], "number": c["number"],
                      "url": f"https://wa.me/{c['number']}" + (f"?text={quote(msg)}" if msg else "")})
    return {"message": template, "links": links, "skipped": skipped}
