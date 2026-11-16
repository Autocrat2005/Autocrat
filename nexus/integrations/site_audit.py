"""
A quick, passive look at a prospect's website: the "we already looked at your stuff" demo, zero tokens.

Only reads what any browser or SEO tool reads: the homepage, robots.txt, sitemap.xml and the size of the
homepage's own scripts (HEAD requests). At most ~10 requests, only for hot leads and booked calls.
Never probes for files, logins, APIs or databases. Private/loopback addresses are refused.

    audit("https://example.com")  ->  {"url", "ok", "facts": {...}, "findings": [{"key", "severity", "line"}]}
    top_findings(result, icp)    ->  the 3 lines worth saying to that kind of buyer
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

UA = "Mozilla/5.0 (compatible; WindroseSiteCheck/1.0)"
MAX_BYTES = 1_500_000

SECURITY_HEADERS = {"strict-transport-security": "HSTS", "content-security-policy": "CSP",
                    "x-frame-options": "clickjacking protection", "x-content-type-options": "nosniff"}
CHAT_HINTS = ("intercom", "crisp.chat", "tawk.to", "drift.com", "zdassets", "zendesk", "hs-scripts", "livechatinc",
              "tidio", "freshchat", "wati.io", "elfsight", "whatsapp-widget", "getbutton.io", "manychat")
BUILDERS = {"Lovable": ("lovable.app", "lovable-tagger", "gptengineer"), "Bolt": ("bolt.new", "stackblitz"),
            "Replit": ("replit.app", "repl.co"), "Wix": ("wixstatic", "wix.com"), "Webflow": ("webflow",),
            "WordPress": ("wp-content", "wp-includes"), "Squarespace": ("squarespace",), "Framer": ("framerusercontent",),
            "Shopify": ("cdn.shopify",), "Bubble": ("bubble.io", "bubbleapps")}
# Which findings each kind of buyer cares about most (extra severity).
ICP_BOOST = {
    "real_estate": {"no_instant_contact": 3, "no_preview": 2, "slow": 1, "no_viewport": 1},
    "insurance": {"no_instant_contact": 3, "slow": 1, "stale_footer": 1, "no_viewport": 1},
    "vibe_coder": {"security_headers": 3, "heavy_js": 2, "not_https": 2, "robots_block": 2, "mixed_content": 1},
}


class Resp:
    def __init__(self, status: int = 0, headers: Optional[Dict[str, str]] = None, text: str = "",
                 elapsed: float = 0.0, url: str = ""):
        self.status, self.text, self.elapsed, self.url = status, text, elapsed, url
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def http_fetch(method: str, url: str, timeout: float = 8.0) -> Resp:
    import httpx
    host = urlparse(url).hostname or ""
    if not host or not _public_host(host):
        return Resp(status=0, url=url)
    start = time.monotonic()
    with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as client:
        if method == "HEAD":
            r = client.head(url)
            return Resp(r.status_code, dict(r.headers), "", time.monotonic() - start, str(r.url))
        with client.stream("GET", url) as r:
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    break
            body = b"".join(chunks)
            return Resp(r.status_code, dict(r.headers), body.decode(r.encoding or "utf-8", "replace"),
                        time.monotonic() - start, str(r.url))


def _find(pattern: str, text: str, flags=re.I | re.S) -> str:
    m = re.search(pattern, text, flags)
    return (m.group(1) if m and m.groups() else (m.group(0) if m else "")).strip()


def audit(url: str, fetch: Optional[Callable[[str, str], Resp]] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    result: Dict[str, Any] = {"url": url, "ok": False, "facts": {}, "findings": []}
    if not urlparse(url).hostname:
        return result
    fetch = fetch or (lambda method, u: http_fetch(method, u))
    year = (now or datetime.now(timezone.utc)).year
    findings: List[Dict[str, Any]] = []
    add = lambda key, sev, line: findings.append({"key": key, "severity": sev, "line": line})  # noqa: E731

    try:
        home = fetch("GET", url)
    except Exception as e:
        result["facts"]["error"] = str(e)[:200]
        return result
    if not home.status or home.status >= 400:
        result["facts"]["status"] = home.status
        if home.status >= 500:
            add("down", 6, f"the homepage returned an error ({home.status}) when we checked")
            result["findings"] = findings
        return result
    result["ok"] = True
    final = home.url or url
    text, low = home.text or "", (home.text or "").lower()
    facts = result["facts"]
    facts.update({"status": home.status, "final_url": final, "load_s": round(home.elapsed, 2),
                  "html_kb": round(len(text.encode("utf-8", "ignore")) / 1024)})
    facts["title"] = re.sub(r"\s+", " ", _find(r"<title[^>]*>(.*?)</title>", text))[:120]
    facts["built_with"] = next((name for name, marks in BUILDERS.items() if any(m in low for m in marks)), "")

    if final.startswith("http://"):
        add("not_https", 5, "the site still loads over plain http, so browsers mark it 'not secure'")
    if home.elapsed > 3.0:
        add("slow", 4, f"the homepage took {home.elapsed:.1f}s to load when we checked (people bail after ~3s)")
    if 'name="viewport"' not in low and "name='viewport'" not in low:
        add("no_viewport", 4, "no mobile viewport tag, so on phones the page shows zoomed out")
    if not _find(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)', text):
        add("no_description", 2, "no meta description, so google shows a random snippet for it")
    if "og:image" not in low:
        add("no_preview", 3, "links shared on whatsapp or linkedin show no preview image")
    copyright_year = _find(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", text)
    if copyright_year and int(copyright_year) < year - 1:
        add("stale_footer", 3, f"the footer still says {copyright_year}, which makes the site look unattended")
    has_wa = "wa.me/" in low or "api.whatsapp.com" in low or "whatsapp://" in low
    has_chat = any(h in low for h in CHAT_HINTS)
    has_tel = "href=\"tel:" in low or "href='tel:" in low
    facts.update({"whatsapp_link": has_wa, "chat_widget": has_chat, "phone_link": has_tel, "form": "<form" in low})
    if not has_wa and not has_chat:
        add("no_instant_contact", 3, "no whatsapp button or live chat, so new enquiries wait for someone to see them")
    if final.startswith("https://") and re.search(r'(?:src|href)=["\']http://(?!localhost)', text, re.I):
        add("mixed_content", 4, "some files load over plain http on the https page (browsers block or warn)")

    missing = [label for h, label in SECURITY_HEADERS.items() if h not in home.headers]
    facts["missing_headers"] = missing
    if len(missing) >= 3:
        add("security_headers", 3, f"basic security headers are missing ({', '.join(missing[:3])})")
    if "x-powered-by" in home.headers:
        facts["powered_by"] = home.headers["x-powered-by"][:60]

    # The homepage's own JavaScript weight (HEAD only).
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', text, re.I)
    host = urlparse(final).hostname or ""
    own = [urljoin(final, s) for s in scripts if not s.startswith("//") and
           (urlparse(urljoin(final, s)).hostname or "") == host][:6]
    js_bytes = 0
    for src in own:
        try:
            r = fetch("HEAD", src)
            js_bytes += int(r.headers.get("content-length") or 0)
        except Exception:
            continue
    facts.update({"scripts": len(scripts), "own_js_kb": round(js_bytes / 1024)})
    if js_bytes > 1_000_000:
        add("heavy_js", 4, f"about {js_bytes / 1_000_000:.1f} MB of javascript loads up front, slow on phones")

    root = f"{urlparse(final).scheme}://{host}"
    try:
        robots = fetch("GET", root + "/robots.txt")
        rtext = (robots.text or "") if robots.status == 200 else ""
        if re.search(r"user-agent:\s*\*\s*(?:\n(?!user-agent).*)*\n\s*disallow:\s*/\s*(?:\n|$)", rtext.lower()):
            add("robots_block", 5, "robots.txt tells google not to index the whole site")
        facts["robots"] = robots.status == 200
    except Exception:
        pass
    try:
        sitemap = fetch("HEAD", root + "/sitemap.xml")
        facts["sitemap"] = sitemap.status == 200
        if sitemap.status in (404, 410):
            add("no_sitemap", 1, "there's no sitemap, so google finds new pages slowly")
    except Exception:
        pass
    result["findings"] = sorted(findings, key=lambda f: -f["severity"])
    return result


def top_findings(result: Dict[str, Any], icp: str = "", n: int = 3) -> List[str]:
    boost = ICP_BOOST.get(icp or "", {})
    ranked = sorted(result.get("findings") or [], key=lambda f: -(f["severity"] + boost.get(f["key"], 0)))
    return [f["line"] for f in ranked[:n]]


def domain_of(url: str) -> str:
    host = urlparse(url if "://" in (url or "") else f"https://{url}").hostname or ""
    return host[4:] if host.startswith("www.") else host
