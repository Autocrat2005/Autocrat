"""The passive site check behind the pre-call pack: real, specific findings, no probing."""

from datetime import datetime, timezone

from nexus.integrations import site_audit
from nexus.integrations.site_audit import Resp

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
SLOW_OLD_SITE = """<html><head><title>Palm Realty | Dubai</title>
<script src="/assets/index-abc.js"></script><script src="https://cdn.other.example/x.js"></script></head>
<body><form action="/contact"></form><img src="http://palm.example/logo.png"><footer>© 2022 Palm Realty</footer></body></html>"""


def fake_fetch(pages):
    calls = []

    def fetch(method, url):
        calls.append((method, url))
        return pages.get((method, url), Resp(404, {}, "", 0.1, url))
    fetch.calls = calls
    return fetch


def test_findings_are_specific_and_ranked_for_the_buyer():
    fetch = fake_fetch({
        ("GET", "https://palm.example"): Resp(200, {"Content-Type": "text/html"}, SLOW_OLD_SITE, 4.2, "https://palm.example/"),
        ("HEAD", "https://palm.example/assets/index-abc.js"): Resp(200, {"Content-Length": "1800000"}, "", 0.1),
        ("GET", "https://palm.example/robots.txt"): Resp(200, {}, "User-agent: *\nDisallow: /\n", 0.1),
    })
    out = site_audit.audit("palm.example", fetch=fetch, now=NOW)
    keys = {f["key"] for f in out["findings"]}
    assert out["ok"] and {"slow", "no_viewport", "stale_footer", "no_preview", "no_instant_contact", "mixed_content",
                          "security_headers", "heavy_js", "robots_block", "no_sitemap", "no_description"} <= keys
    assert ("HEAD", "https://cdn.other.example/x.js") not in fetch.calls     # only their own scripts are sized
    assert all(m in ("GET", "HEAD") for m, _ in fetch.calls) and len(fetch.calls) <= 10
    broker = site_audit.top_findings(out, "real_estate")
    assert broker[0].startswith("no whatsapp button or live chat")
    founder = site_audit.top_findings(out, "vibe_coder")
    assert any("security headers" in x for x in founder)
    assert all("—" not in x and "–" not in x for x in broker + founder)


def test_a_clean_modern_site_has_little_to_say():
    good = ('<html><head><meta name="viewport" content="width=device-width"><meta name="description" content="x">'
            '<meta property="og:image" content="/og.png"></head><body><a href="https://wa.me/971500000000">chat</a>'
            '<footer>© 2026</footer></body></html>')
    headers = {"Strict-Transport-Security": "max-age=1", "Content-Security-Policy": "default-src 'self'",
               "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}
    fetch = fake_fetch({("GET", "https://good.example"): Resp(200, headers, good, 0.4, "https://good.example/"),
                        ("HEAD", "https://good.example/sitemap.xml"): Resp(200, {}, "", 0.1)})
    out = site_audit.audit("https://good.example", fetch=fetch, now=NOW)
    assert out["ok"] and out["findings"] == [] and out["facts"]["whatsapp_link"]


def test_down_or_missing_sites_and_private_addresses():
    fetch = fake_fetch({})
    assert site_audit.audit("https://gone.example", fetch=fetch, now=NOW)["ok"] is False
    assert site_audit.http_fetch("GET", "http://127.0.0.1:9/").status == 0      # never touches local machines
    assert site_audit.domain_of("https://www.palm.example/x") == "palm.example"
