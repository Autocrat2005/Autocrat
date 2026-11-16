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

    def apply_outcome(self, lead, incoming, decision, alert=True):
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


# ── growth: warm-up cap, bounces, follow-ups ──────────────────────

def sent_opener(store, w, t, email="joe@startup.com", key="g1", days_ago=0.0):
    """Stage + send one opener through the worker, then age it."""
    import time as _t
    lead, out = staged_email_lead(store, email=email, key=key)
    store.update_lead_fields(lead["id"], market="UAE")
    assert w.send_openers(IN_HOURS) >= 1
    if days_ago:
        with store.transaction() as c:
            c.execute("UPDATE outreach SET sent_at=? WHERE id=?", (_t.time() - days_ago * 86400, out["id"]))
    return store.get_lead(lead["id"]), out


def test_cap_ramps_with_clean_days_and_drops_on_bounces(store):
    import time as _t
    w = make_worker(store, FakeTransport(), cap=20)
    w.cap_max, w.cap_step = 40, 3
    assert w.cap_status()["cap"] == 20
    now = _t.time()
    with store.transaction() as c:   # 4 past days with 6 clean sends each
        for d in range(1, 5):
            for i in range(6):
                lead_id = f"L{d}{i}"
                c.execute("INSERT INTO leads(id, dedupe_key, source, state, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                          (lead_id, lead_id, "t", "SENT", now, now))
                c.execute("INSERT INTO outreach(id, lead_id, channel, kind, contact_key, idempotency_key, draft, status,"
                          " created_at, sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (lead_id, lead_id, "email", "first_touch", f"email:{lead_id}@x.com", lead_id, "x", "sent",
                           now, now - d * 86400 - 60))
    st = w.cap_status()
    assert st["clean_days"] == 4 and st["cap"] == 32
    for i in range(3):   # 3 bounces in the last week on 24 sends = 12.5% → back to base
        store.log_event(f"L1{i}", "bounce", {"address": "x"})
    assert w.cap_status()["cap"] == 20


def test_bounce_suppresses_and_stops_followups(store):
    RuntimeControl().set_run_mode("live", actor="test")
    t = FakeTransport()
    w = make_worker(store, t)
    lead, _ = sent_opener(store, w, t)
    bounce = {"uid": "9", "from": "mailer-daemon@zoho.in", "subject": "Undelivered Mail Returned to Sender",
              "message_id": "", "references": "", "body": "Delivery to joe@startup.com failed permanently.",
              "dsn": "Final-Recipient: rfc822; joe@startup.com"}
    t.inbox = [bounce]
    w.handle_replies()
    assert store.is_suppressed("email:joe@startup.com") and store.get_lead(lead["id"])["state"] == "LOST"
    assert "9" in t.seen
    assert w.stage_followups() == 0


def test_followup_threads_into_the_opener_and_stops_on_reply(store, monkeypatch):
    import time as _t
    RuntimeControl().set_run_mode("live", actor="test")
    t = FakeTransport()
    w = make_worker(store, t)
    lead, opener = sent_opener(store, w, t, days_ago=3.5)
    monkeypatch.setattr(w, "draft_followup", lambda lead, n, text: f"bump {n}: happy to look at one flow free?")
    assert w.stage_followups() == 1
    assert w.stage_followups() == 0                                   # one per kind, ever
    assert w.send_openers(IN_HOURS) == 1
    bump = t.sent[-1]
    assert bump["subject"] == "Re: quick one" and bump["in_reply_to"] and bump["body"].startswith("bump 1")
    # second follow-up: after another 4 days, unless they reply
    with store.transaction() as c:
        c.execute("UPDATE outreach SET sent_at=? WHERE lead_id=? AND kind='followup_1'", (_t.time() - 5 * 86400, lead["id"]))
    store.log_event(lead["id"], "message_in", {"text": "sure, tell me more"}, actor="prospect")
    assert w.stage_followups() == 0


def test_placeholder_or_replied_followup_never_sends(store, monkeypatch):
    RuntimeControl().set_run_mode("live", actor="test")
    t = FakeTransport()
    w = make_worker(store, t)
    lead, _ = sent_opener(store, w, t, days_ago=4)
    monkeypatch.setattr(w, "draft_followup", lambda lead, n, text: "hey [first name], bump")
    assert w.stage_followups() == 0
    monkeypatch.setattr(w, "draft_followup", lambda lead, n, text: "fair bump")
    assert w.stage_followups() == 1
    store.log_event(lead["id"], "message_in", {"text": "we're good thanks"}, actor="prospect")
    before = len(t.sent)
    w.send_openers(IN_HOURS)
    assert len(t.sent) == before                                       # replied since: follow-up dropped
    assert all(o["status"] != "staged" for o in store.outreach_for_lead(lead["id"]))


def test_every_reply_alerts_once_with_their_words_and_our_answer(store):
    RuntimeControl().set_run_mode("live", actor="test")
    staged_email_lead(store)
    inbox = [{"uid": "5", "from": "broker@agency.ae", "subject": "Re: quick one", "message_id": "<r>", "references": "",
              "body": "we lose leads at night honestly, what does it cost?"}]
    t = FakeTransport(inbox)
    w = make_worker(store, t, {"reply": "most setups start at $2,500. how many leads a week?", "intent": "pricing",
                               "hot": True, "summary": "asked price"})
    w.handle_replies()
    alerts = [text for prio, text in w.alerts.items]
    assert len(alerts) == 1 and w.alerts.items[0][0] == "red"
    assert "HOT email reply" in alerts[0] and "what does it cost" in alerts[0] and "auto-replied" in alerts[0]
