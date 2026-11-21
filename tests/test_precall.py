"""Pre-call pack: one cheap brief per hot lead or booked call, a site check, an optional note for them."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nexus.core.runtime import RuntimeControl
from nexus.integrations import precall
from nexus.integrations.email_channel import EmailWorker
from nexus.integrations.site_audit import Resp
from tests.test_conversion import RecentTransport, sent_lead
from tests.test_email_channel import FakeAlerts, FakeCloser
from tests.test_site_audit import SLOW_OLD_SITE, fake_fetch


class Router:
    def __init__(self):
        self.calls = 0

    def complete(self, *a, **k):
        self.calls += 1
        return SimpleNamespace(data={"who": "two-agent brokerage in Dubai Marina", "pain": "after-hours leads go cold",
                                     "sell": "every enquiry answered in seconds", "demo": "their listings in a WhatsApp flow",
                                     "price": "from $2,500, 50% to start", "objections": ["too expensive: one lost deal costs more"],
                                     "first_question": "how many enquiries a week?"})


def worker(store):
    closer = FakeCloser({"reply": "", "intent": "other"}, store)
    closer.router = Router()
    w = EmailWorker(store, closer, FakeAlerts(), transport=RecentTransport(), sleep=lambda s: None)
    w.auto_send, w.daily_cap, w.hours, w.offset = True, 20, "9-18", 240
    return w


def test_site_for_prefers_their_own_site():
    lead = {"url": "https://news.ycombinator.com/item?id=1", "contact": "founder@acme.example", "data": {}}
    assert precall.site_for(lead) == "https://acme.example"
    assert precall.site_for({**lead, "data": {"website": "https://acme.example/app"}}) == "https://acme.example/app"
    assert precall.site_for({"url": "", "contact": "me@gmail.com", "data": {}}) == ""


def test_one_pack_per_booked_call_with_a_note_you_can_send(store):
    RuntimeControl().set_run_mode("live", actor="test")
    w = worker(store)
    lead, _ = sent_lead(store, w, w.transport, email="sara@palm.example", key="pc1")
    store.update_lead_fields(lead["id"], niche="real_estate", data={"website": "https://palm.example"})
    start = datetime.now(timezone.utc) + timedelta(hours=3)
    store.log_event(lead["id"], "meeting_booked", {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "link": "https://x",
                                                   "their_offset": 240}, actor="closer")
    fetch = fake_fetch({("GET", "https://palm.example"): Resp(200, {}, SLOW_OLD_SITE, 4.0, "https://palm.example/")})
    assert precall.prepare_packs(w, fetch=fetch) == 1
    assert precall.prepare_packs(w, fetch=fetch) == 0                     # once per lead, never re-billed
    assert w.closer.router.calls == 1
    priority, text = w.alerts.items[-1]
    assert priority == "red" and "Pre-call pack" in text and "their listings in a WhatsApp flow" in text
    assert "no whatsapp button or live chat" in text
    assert w.alerts.buttons == [[{"text": "📤 Send them the 'ahead of our call' note", "callback_data": f"pt:{lead['id']}"}]]
    note = precall.send_teaser(store, w, lead["id"])
    body = w.transport.sent[-1]["body"]
    assert note.startswith("📤 Sent") and "i had a quick look at palm.example" in body and body.count("\n- ") >= 2
    assert "—" not in body and "–" not in body
    assert precall.send_teaser(store, w, lead["id"]) == "Already sent."


def test_hot_reply_without_a_call_gets_a_pack_too(store):
    RuntimeControl().set_run_mode("live", actor="test")
    w = worker(store)
    lead, _ = sent_lead(store, w, w.transport, email="kim@nest.example", key="pc2")
    store.log_event(lead["id"], "message_in", {"text": "how much is it?", "hot": True}, actor="prospect")
    assert precall.leads_needing_pack(store) == [lead["id"]]
    assert precall.prepare_packs(w, fetch=fake_fetch({})) == 1
    assert "hot, no call yet" in w.alerts.items[-1][1]
