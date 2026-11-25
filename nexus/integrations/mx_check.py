"""
Can this domain receive email at all? Asked before we draft or send, so dead addresses never bounce
(bounces are what hold the daily sending limit down).

A public DNS-over-HTTPS lookup of the domain's MX (then A) record: free, no tokens, cached in the store.
If the lookup itself fails we assume the domain is fine: a network blip must never block sending.
Set MX_CHECK=0 to switch it off.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from nexus.core.logger import get_logger
from nexus.core.secrets import env_bool

log = get_logger("mx")

DOH_URL = "https://cloudflare-dns.com/dns-query"
BIG_PROVIDERS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
                 "me.com", "aol.com", "proton.me", "protonmail.com", "zoho.com", "msn.com", "yahoo.co.uk",
                 "yahoo.co.in", "ymail.com", "hotmail.co.uk", "mail.com", "gmx.com"}
# Common typos of the big providers: these never deliver.
TYPOS = {"gmial.com", "gmai.com", "gamil.com", "gmail.co", "gmail.con", "gnail.com", "gmaill.com", "gmal.com",
         "hotmial.com", "hotmai.com", "hotmail.con", "yahooo.com", "yaho.com", "yahoo.con", "outlok.com",
         "outlook.con", "icloud.con", "gmail.cm", "gmail.om"}
OK_TTL, BAD_TTL = 30 * 86400, 3 * 86400

# Example addresses copied off sign-up forms and docs ("you@company.com", "jane@company.com"): they always bounce.
PLACEHOLDER_LOCALS = {"you", "your", "yourname", "your.name", "name", "email", "youremail", "johndoe", "john.doe",
                      "jane.doe", "janedoe", "jdoe", "user", "username", "test", "example", "someone", "firstname",
                      "first.last", "firstname.lastname", "myname", "sample"}
# "email me at my-last-name@…", "firstname.lastname@…", "first_initial+last@…": a pattern to fill in, not an inbox.
PLACEHOLDER_LOCAL_RE = re.compile(r"^(?:my|your)?[-_.]?(?:first|last|full|given|sur)?[-_.]?(?:initial|name)s?"
                                  r"(?:[-_.+]?(?:first|last|sur)?[-_.]?(?:initial|name)s?)?$")
PLACEHOLDER_DOMAINS = {"company.com", "domain.com", "example.com", "example.org", "example.net", "yourdomain.com",
                       "yourcompany.com", "website.com", "yoursite.com", "acme.com", "email.com", "test.com",
                       "mysite.com", "sentry.io", "wixpress.com", "sentry.wixpress.com"}
# The same sample names on any ending: "lee@acme.dev", "hi@example.io", "jane@mail.yourcompany.co.uk".
PLACEHOLDER_NAMES = {"acme", "example", "yourcompany", "yourdomain", "mycompany", "companyname", "yoursite", "mysite",
                     "yourwebsite", "domain", "company", "website", "sample", "test", "foo", "foobar"}
TWO_PART_ENDINGS = {"co.uk", "org.uk", "com.au", "co.in", "co.nz", "com.br", "co.za", "com.sg", "co.jp", "com.mx"}


def domain_name(domain: str) -> str:
    """The name part of a domain, without its ending: mail.acme.co.uk → acme."""
    parts = [p for p in domain.lower().strip(".").split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_ENDINGS:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""
# Inboxes that answer with a canned ticket or don't exist on a young product: wrong people, frequent bounces.
ROLE_LOCALS = ("support", "help", "helpdesk", "legal", "privacy", "security", "abuse", "noreply", "no-reply",
               "donotreply", "billing", "careers", "jobs", "press", "dpo", "compliance")


# Pulled from page source where ">" was written as backslash-u003e: the address came out as "u003esupport@x.com".
# Two such addresses bounced on 2026-11-25.
ESCAPED_LOCAL_RE = re.compile(r"^(?:u00[0-9a-f]{2}|x(?:3c|3e|22|27|26))")


def is_unusable_address(address: str) -> str:
    """Why this address must not get a cold email ('' = fine)."""
    local, _, domain = (address or "").strip().lower().partition("@")
    if not local or not domain:
        return "not an address"
    if ESCAPED_LOCAL_RE.match(local):
        return "a broken address (an escaped character stuck to the front)"
    if domain in PLACEHOLDER_DOMAINS or domain_name(domain) in PLACEHOLDER_NAMES \
            or local in PLACEHOLDER_LOCALS or PLACEHOLDER_LOCAL_RE.match(local):
        return "an example address from a form or docs"
    if any(local == r or local.startswith(r + ".") or local.startswith(r + "-") or local.startswith(r + "_")
           for r in ROLE_LOCALS):
        return "a help-desk or legal inbox"
    return ""


def doh_lookup(domain: str, record: str) -> Optional[list]:
    """Answers for a record type, [] when there are none, None when the lookup failed."""
    import httpx
    try:
        r = httpx.get(DOH_URL, params={"name": domain, "type": record}, timeout=8,
                      headers={"accept": "application/dns-json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug(f"dns lookup for {domain} failed: {e}")
        return None
    if data.get("Status") == 3:        # NXDOMAIN: the domain doesn't exist
        return []
    if data.get("Status") != 0:
        return None
    want = {"MX": 15, "A": 1}.get(record)
    return [a.get("data", "") for a in data.get("Answer") or [] if a.get("type") == want]


def has_mail_server(domain: str, store=None, lookup: Optional[Callable[[str, str], Optional[list]]] = None) -> bool:
    domain = (domain or "").split("@")[-1].strip().lower().rstrip(".")
    if not domain or "." not in domain or domain in TYPOS:
        return False
    if domain in BIG_PROVIDERS or not env_bool("MX_CHECK", True):
        return True
    key = f"mx:{domain}"
    if store is not None:
        cached = store.kv_get(key) or ""
        verdict, _, stamp = cached.partition(":")
        if verdict in ("1", "0") and stamp and time.time() - float(stamp) < (OK_TTL if verdict == "1" else BAD_TTL):
            return verdict == "1"
    lookup = lookup or doh_lookup
    mx = lookup(domain, "MX")
    if mx is None:
        return True                    # couldn't check: don't block
    real = [m for m in mx if m.split()[-1].strip(".")]       # "0 ." is a null MX: accepts no mail
    if mx and not real:
        ok = False
    elif real:
        ok = True
    else:
        a = lookup(domain, "A")        # no MX: mail falls back to the A record, if there is one
        if a is None:
            return True
        ok = bool(a)
    if store is not None:
        store.kv_set(key, f"{'1' if ok else '0'}:{int(time.time())}")
    return ok
