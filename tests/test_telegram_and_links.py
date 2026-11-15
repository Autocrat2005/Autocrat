from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from nexus.core.deeplinks import compose_links, wa_me_url, whatsapp_web_url
from nexus.integrations.telegram_alerts import approved_buttons, build_alert, in_quiet_hours
from nexus.integrations.telegram_bot import TelegramBot


def test_user_id_allowlist_not_chat_id():
    bot = TelegramBot("t", allowed_user_ids=["111", "222"], allowed_chat_ids=[])
    assert bot.is_authorized_ids(111, 999)          # DM or any chat
    assert not bot.is_authorized_ids(333, 999)     # stranger
    assert not bot.is_authorized_ids(None, 999)


def test_chat_restriction_applies_on_top_of_users():
    bot = TelegramBot("t", allowed_user_ids=["111"], allowed_chat_ids=["-100500"])
    assert bot.is_authorized_ids(111, -100500)
    assert not bot.is_authorized_ids(111, 42)       # right person, wrong chat
    assert not bot.is_authorized_ids(333, -100500)  # group member who isn't a founder


def test_empty_allowlist_denies_everyone():
    bot = TelegramBot("t", allowed_user_ids=[], allowed_chat_ids=[])
    assert not bot.is_authorized_ids(111, 111)


def test_whatsapp_links_encode_draft():
    draft = "Hi Dr. Rao — built you a demo: https://x.example/d?a=1&b=2"
    web = urlparse(whatsapp_web_url("+971 50 123 4567", draft))
    assert web.netloc == "web.whatsapp.com" and web.path == "/send"
    q = parse_qs(web.query)
    assert q["phone"] == ["971501234567"] and q["text"] == [draft]
    phone = urlparse(wa_me_url("+971501234567", draft))
    assert phone.netloc == "wa.me" and phone.path == "/971501234567"
    assert parse_qs(phone.query)["text"] == [draft]
    links = compose_links("whatsapp", "+971501234567", draft)
    assert links["desk"].startswith("https://web.whatsapp.com/") and links["phone"].startswith("https://wa.me/")


def test_staged_alert_has_approve_button(store):
    lead, _ = store.upsert_lead(source="t", dedupe_key="k", name="Dubai Clinic", channel="whatsapp",
                                contact="+971500000001", market="UAE")
    store.transition(lead["id"], "TRIAGED")
    store.transition(lead["id"], "BUILDING")
    out = store.stage_outreach(lead["id"], "whatsapp", "Hello <b>there</b>")
    t = store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    alert = build_alert({"event": "lead.transition", "data": t}, store=store)
    assert alert["priority"] == "orange"
    assert alert["buttons"][0][0]["callback_data"] == f"oa:{out['id']}"
    assert "&lt;b&gt;" in alert["text"]          # draft is HTML-escaped
    after = approved_buttons(out["id"], compose_links("whatsapp", "+971500000001", "hi"))
    assert after[0][0]["url"].startswith("https://wa.me/") and after[0][1]["callback_data"] == f"os:{out['id']}"
    assert all(len(b.get("callback_data", "")) <= 64 for row in after for b in row)


def test_red_alerts():
    for state in ("FORWARDED", "DEPOSIT_OPENED", "REPLIED", "WON"):
        alert = build_alert({"event": "lead.transition", "data": {"to": state, "lead_id": None}})
        assert alert["priority"] == "red"
    kill = build_alert({"event": "control.kill_switch", "data": {"engaged": True, "actor": "x"}})
    assert kill["priority"] == "red"
    assert build_alert({"event": "lead.transition", "data": {"to": "TRIAGED"}}) is None


def test_quiet_hours_ist():
    late = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)   # 23:30 IST
    day = datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)     # 11:30 IST
    assert in_quiet_hours("23-8", late) and not in_quiet_hours("23-8", day)


def test_reports_become_alerts_and_long_messages_split():
    alert = build_alert({"event": "report.opportunities", "data": {"text": "🧭 pains <b>"}})
    assert alert["priority"] == "orange" and "&lt;b&gt;" in alert["text"]
    long = "\n".join("line %d %s" % (i, "x" * 80) for i in range(200))
    parts = TelegramBot._chunks(long)
    assert len(parts) > 1 and all(len(p) <= 3900 for p in parts)
    assert "\n".join(parts) == long
