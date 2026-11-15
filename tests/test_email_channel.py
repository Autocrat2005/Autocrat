"""Email channel against a fake mailbox: openers, caps, hours, threaded AI replies, DNC."""

from datetime import datetime, timezone

from nexus.core.runtime import RuntimeControl
from nexus.integrations.email_channel import AlertFanout, EmailWorker, split_subject, strip_quoted


class FakeTransport:
    configured = True

    def __init__(self, inbox=None):
        self.sent, self.inbox, self.seen = [], list(inbox or []), []

    def send(self, to, subject, body, in_reply_to="", references=""):
        self.sent.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})
        return f"<m{len(self.sent)}@x>"

    def fetch_unseen(self, limit=20, wanted=None):
        return [m for m in self.inbox if m["uid"] not in self.seen]

    def mark_seen(self, uid):
        self.seen.append(uid)


class FakeAlerts:
    groups, numbers = [], []

    def __init__(self):
        self.items = []

    def notify(self, text, priority="orange"):
        self.items.append((priority, text))


class FakeCloser:
    def __init__(self, decision, store):
        self.decision, self.store, self.calls = decision, store, []

    def decide(self, transcript, lead_id=None, channel="WhatsApp"):
        self.calls.append((transcript, channel))
        return dict(self.decision)

    def apply_outcome(self, lead, incoming, decision):
        self.store.log_event(lead["id"], "message_in", {"text": incoming})
        if decision["intent"] == "stop":
            self.store.transition(lead["id"], "DNC", reason="stop")


IN_HOURS = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)      # 11:00 Dubai
NIGHT = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)        # 00:00 Dubai


def staged_email_lead(store, email="broker@agency.ae", key="list:1"):
    lead, _ = store.upsert_lead(source="list", dedupe_key=key, name="Sara", channel="email", contact=email)
    store.transition(lead["id"], "TRIAGED")
    out = store.stage_outreach(lead["id"], "email", "subject: quick one\nhey sara, how do whatsapp enquiries reach you?")
    store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    return lead, out


def make_worker(store, transport, closer_decision=None, auto=True, cap=20):
    w = EmailWorker(store, FakeCloser(closer_decision or {"reply": "", "intent": "other"}, store), FakeAlerts(),
                    transport=transport, sleep=lambda s: None)
    w.auto_send, w.daily_cap, w.hours, w.offset = auto, cap, "9-18", 240
    return w


def test_subject_and_quote_helpers():
    assert split_subject("subject: hi there\nbody text") == ("hi there", "body text")
    assert split_subject("no subject line")[0] == "quick question"
    reply = "sounds good, how much?\n\nOn Mon, 16 Nov 2026 Yash wrote:\n> hey sara"
    assert strip_quoted(reply) == "sounds good, how much?"


def test_live_auto_send_with_optout_and_cap(store):
    RuntimeControl().set_run_mode("live", actor="test")
    lead, _ = staged_email_lead(store)
    staged_email_lead(store, email="two@agency.ae", key="list:2")
    t = FakeTransport()
    w = make_worker(store, t, cap=1)
    assert w.send_openers(IN_HOURS) == 1                       # daily cap respected
    assert t.sent[0]["subject"] == "quick one" and "reply 'no'" in t.sent[0]["body"]
    assert store.get_lead(lead["id"])["state"] == "SENT"
    assert w.send_openers(IN_HOURS) == 0


def test_placeholder_drafts_never_send(store):
    RuntimeControl().set_run_mode("live", actor="test")
    lead, _ = store.upsert_lead(source="x", dedupe_key="ph", name="", channel="email", contact="a@b.ae")
    store.transition(lead["id"], "TRIAGED")
    out = store.stage_outreach(lead["id"], "email", "subject: hi\nhey [first name], quick one")
    store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    t = FakeTransport()
    assert make_worker(store, t).send_openers(IN_HOURS) == 0 and t.sent == []


def test_no_sends_at_night_or_in_dry_run(store):
    staged_email_lead(store)
    t = FakeTransport()
    w = make_worker(store, t)
    assert w.send_openers(NIGHT) == 0
    assert RuntimeControl().is_dry_run and w.send_openers(IN_HOURS) == 0 and t.sent == []


def test_without_auto_send_only_approved_go_out(store):
    RuntimeControl().set_run_mode("live", actor="test")
    _, out = staged_email_lead(store)
    t = FakeTransport()
    w = make_worker(store, t, auto=False)
    assert w.send_openers(IN_HOURS) == 0
    store.approve_outreach(out["id"], actor="telegram:1")
    assert w.send_openers(IN_HOURS) == 1


def test_reply_is_answered_in_thread(store):
    RuntimeControl().set_run_mode("live", actor="test")
    lead, _ = staged_email_lead(store)
    inbox = [{"uid": "7", "from": "broker@agency.ae", "subject": "Re: quick one", "message_id": "<their@x>",
              "references": "", "body": "honestly we miss a lot at night. how much is this?\n\nOn Mon Yash wrote:\n> hey"}]
    t = FakeTransport(inbox)
    w = make_worker(store, t, {"reply": "most setups start at $2,500, half to start. what's your volume like?",
                               "intent": "pricing", "hot": True, "summary": "asked price"})
    assert w.handle_replies() == 1
    sent = t.sent[-1]
    assert sent["to"] == "broker@agency.ae" and sent["subject"] == "Re: quick one" and sent["in_reply_to"] == "<their@x>"
    transcript, channel = w.closer.calls[0]
    assert channel == "email" and "how much is this?" in transcript and "On Mon" not in transcript
    assert t.seen == ["7"]


def test_unknown_senders_left_unread_and_stop_means_dnc(store):
    RuntimeControl().set_run_mode("live", actor="test")
    staged_email_lead(store)
    inbox = [{"uid": "1", "from": "newsletter@spam.com", "subject": "deals", "message_id": "", "references": "", "body": "x"},
             {"uid": "2", "from": "broker@agency.ae", "subject": "Re: quick one", "message_id": "<a>", "references": "",
              "body": "no thanks"}]
    t = FakeTransport(inbox)
    w = make_worker(store, t, {"reply": "no worries, all the best", "intent": "stop"})
    w.handle_replies()
    assert "1" not in t.seen                                     # not ours: left unread
    assert store.find_lead_by_contact("email:broker@agency.ae")["state"] == "DNC"


def test_fanout_reaches_every_channel():
    a, b = FakeAlerts(), FakeAlerts()
    AlertFanout([a, None, b]).notify("hi", "red")
    assert a.items == b.items == [("red", "hi")]


def test_each_lead_gets_its_own_business_hours(store):
    from nexus.integrations.email_channel import market_offset
    assert market_offset("US", 240) == -300 and market_offset("Canada", 240) == -300
    assert market_offset("UAE", 0) == 240 and market_offset("unknown", 240) == 240
    RuntimeControl().set_run_mode("live", actor="test")
    uae, _ = staged_email_lead(store, email="b@dubai.ae", key="k1")
    us, _ = store.upsert_lead(source="hn", dedupe_key="k2", name="Joe", channel="email", contact="joe@startup.com",
                              market="US")
    store.transition(us["id"], "TRIAGED")
    out = store.stage_outreach(us["id"], "email", "subject: hi\nhey joe")
    store.transition(us["id"], "STAGED", payload={"outreach_id": out["id"]})
    t = FakeTransport()
    w = make_worker(store, t)
    at = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
    w.postal = ""
    assert w.send_openers(at) == 0                               # US held: no postal address yet
    w.postal = "Windrose Labs, 1 Example St, Pune, India"
    # 15:00 UTC = 19:00 Dubai (closed) and 10:00 New York (open): only the US lead goes out
    assert w.send_openers(at) == 1
    assert t.sent[0]["to"] == "joe@startup.com" and t.sent[0]["body"].endswith("1 Example St, Pune, India")
