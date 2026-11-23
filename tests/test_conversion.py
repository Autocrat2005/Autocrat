"""Cash and conversion: replies are never missed, calls get booked with real times, quiet threads get nudged."""

from datetime import datetime, timedelta, timezone

from nexus.core.runtime import RuntimeControl
from nexus.integrations import booking
from nexus.integrations.brief import daily_brief
from nexus.integrations.email_channel import EmailWorker, html_to_text, is_auto_reply, parse_mail
from tests.test_email_channel import FakeAlerts, FakeCloser, staged_email_lead

IN_HOURS = datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)      # 11:00 Dubai


class RecentTransport:
    """Like Zoho: recent mail incl. already-read, a Sent folder, attachments."""
    configured, address = True, "yash@causalith.com"

    def __init__(self, inbox=None, answered=()):
        self.inbox, self.sent, self.seen, self.answered = list(inbox or []), [], [], set(answered)

    def fetch_recent(self, days=3, limit=40, wanted=None):
        return [m for m in self.inbox if not wanted or wanted(m)]

    def answered_by_me(self, message_id):
        return message_id in self.answered

    def mark_seen(self, uid):
        self.seen.append(uid)

    def send(self, to, subject, body, in_reply_to="", references="", ics=""):
        self.sent.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to, "ics": ics})
        return f"<out{len(self.sent)}@causalith.com>"


def worker(store, t, decision=None):
    w = EmailWorker(store, FakeCloser(decision or {"reply": "fair, who handles product there?", "intent": "question",
                                                   "hot": False}, store), FakeAlerts(), transport=t, sleep=lambda s: None)
    w.auto_send, w.daily_cap, w.hours, w.offset = True, 20, "9-18", 240
    return w


def sent_lead(store, w, t, email="founder@saas.example", key="v1"):
    lead, _ = staged_email_lead(store, email=email, key=key)
    store.update_lead_fields(lead["id"], market="UAE")
    w.send_openers(IN_HOURS)
    return store.get_lead(lead["id"]), t.sent[-1]


def mail(**kw):
    base = {"uid": "1", "seen": False, "from": "founder@saas.example", "subject": "Re: quick one", "message_id": "<r1@x>",
            "in_reply_to": "", "references": "", "body": "", "dsn": "", "auto": False}
    base.update(kw)
    return base


def test_html_only_and_already_read_reply_is_answered(store):
    RuntimeControl().set_run_mode("live", actor="test")
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t)
    html = "<html><body><p>Hi there,</p><p>Thanks for reaching out. Check our security guidelines.</p></body></html>"
    raw = (b"From: Support <founder@saas.example>\r\nSubject: Reply from Support Team\r\nMessage-ID: <v@saas.example>\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n" + html.encode())
    parsed = parse_mail(raw, "114", "(\\Seen)")
    assert parsed["seen"] and "security guidelines" in parsed["body"] and not parsed["auto"]
    t.inbox = [parsed]
    assert w.handle_replies() == 1
    assert t.sent[-1]["to"] == "founder@saas.example" and t.sent[-1]["in_reply_to"] == "<v@saas.example>"
    assert any(e["type"] == "message_in" and "security guidelines" in e["payload"]["text"]
               for e in store.lead_events(lead["id"]))
    assert w.handle_replies() == 0                    # tracked by Message-ID: never answered twice


def test_reply_from_a_colleague_matches_by_thread_and_domain(store):
    RuntimeControl().set_run_mode("live", actor="test")
    t = RecentTransport()
    w = worker(store, t)
    lead, opener = sent_lead(store, w, t, email="hello@brightloop.example", key="a1")
    our_id = store.lead_events(lead["id"])[-1]["payload"]["message_id"]
    t.inbox = [mail(uid="5", **{"from": "cto@brightloop.example"}, message_id="<c@brightloop.example>", in_reply_to=our_id, body="send details")]
    assert w.handle_replies() == 1 and t.sent[-1]["to"] == "cto@brightloop.example"
    t.inbox = [mail(uid="6", **{"from": "someone@gmail.com"}, message_id="<g@x>", body="hi")]
    assert w.handle_replies() == 0                    # free-mail domains never match by domain


def test_auto_replies_and_hand_answered_mail_get_no_auto_answer(store):
    RuntimeControl().set_run_mode("live", actor="test")
    t = RecentTransport(answered={"<h@x>"})
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="omar@palm.example", key="p1")
    before = len(t.sent)
    t.inbox = [mail(uid="7", **{"from": "omar@palm.example"}, message_id="<o@x>", subject="Out of office", body="away", auto=True),
               mail(uid="8", **{"from": "omar@palm.example"}, message_id="<h@x>", seen=True, body="call me tomorrow")]
    w.handle_replies()
    assert len(t.sent) == before                      # nothing auto-sent
    types = [e["type"] for e in store.lead_events(lead["id"])]
    assert "auto_reply" in types and "message_in" in types
    assert store.get_lead(lead["id"])["state"] == "REPLIED"
    assert "already answered" in w.alerts.items[-1][1]


def test_auto_reply_detection():
    from email.message import EmailMessage
    m = EmailMessage(); m["Subject"] = "Automatic reply: quick one"
    assert is_auto_reply(m)
    m2 = EmailMessage(); m2["Subject"] = "Re: quick one"; m2["Auto-Submitted"] = "no"
    assert not is_auto_reply(m2)
    assert html_to_text("<p>a</p><br>b &amp; c") == "a\nb & c"


def test_slots_respect_both_calendars_and_bookings(monkeypatch):
    monkeypatch.setenv("OWNER_AVAILABILITY", "Mon-Sat 10-22")
    monkeypatch.setenv("OWNER_UTC_OFFSET_MINUTES", "330")
    now = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)          # Mon 09:30 IST
    slots = booking.open_slots(240, now=now)                          # Dubai prospect
    assert len(slots) == 4
    for s in slots:
        ist, dubai = s + timedelta(minutes=330), s + timedelta(minutes=240)
        assert 10 <= ist.hour < 22 and 9 <= dubai.hour < 18 and dubai.weekday() < 5 and s >= now + timedelta(hours=3)
    busy = booking.open_slots(240, now=now, busy=[slots[0]])
    assert slots[0] not in busy
    assert booking.parse_meeting(slots[1].strftime("%Y-%m-%dT%H:%M:%SZ"), slots) == slots[1]
    assert booking.parse_meeting("2026-12-25T10:00:00Z", slots) is None   # not a time we offered


def test_meeting_booked_sends_invite_and_reminders(store):
    RuntimeControl().set_run_mode("live", actor="test")
    t = RecentTransport()
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=50)
    w = worker(store, t, {"reply": "locked in, see you then", "intent": "call", "hot": True,
                          "meeting": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "meeting_link": "https://meet.jit.si/windrose-x",
                          "their_offset": 240})
    lead, _ = sent_lead(store, w, t, email="sara@dune.example", key="d1")

    class Booker(FakeCloser):
        def apply_outcome(self, lead, incoming, decision, alert=True):
            super().apply_outcome(lead, incoming, decision, alert)
            store.log_event(lead["id"], "meeting_booked", {"start": decision["meeting"], "link": decision["meeting_link"],
                                                           "their_offset": 240})
    w.closer = Booker(w.closer.decision, store)
    t.inbox = [mail(uid="9", **{"from": "sara@dune.example"}, message_id="<s@dune.example>", body="tue 10:30 works")]
    w.handle_replies()
    assert "BEGIN:VCALENDAR" in t.sent[-1]["ics"] and "meet.jit.si/windrose-x" in t.sent[-1]["ics"]
    assert "MEETING BOOKED" in w.alerts.items[-1][1]
    assert w.meeting_reminders() == 1                  # 60-min reminder (50 min away)
    assert "reminder" in t.sent[-1]["body"] and t.sent[-1]["to"] == "sara@dune.example"
    assert w.meeting_reminders() == 0                  # once only


def test_quiet_conversation_gets_one_nudge(store, monkeypatch):
    RuntimeControl().set_run_mode("live", actor="test")
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="kim@nest.example", key="n1")
    store.log_event(lead["id"], "message_in", {"text": "interesting, what does it cost?"}, actor="prospect")
    store.transition(lead["id"], "REPLIED")
    store.log_event(lead["id"], "message_out", {"text": "from $1,250. what's your volume?", "channel": "email"}, actor="closer")
    with store.transaction() as c:
        # Relative to the test's own clock, not the machine's, so the test doesn't age.
        c.execute("UPDATE lead_events SET ts = ? WHERE lead_id=?", (IN_HOURS.replace(day=17).timestamp() - 3 * 86400, lead["id"]))

    class R:
        def complete(self, *a, **k):
            from types import SimpleNamespace
            return SimpleNamespace(data={"draft": "still keen to sort the volume question? happy to jump on a quick call"})
    w.closer.router = R()
    assert w.nudge_quiet_conversations(now=IN_HOURS.replace(day=17)) == 1
    assert t.sent[-1]["to"] == "kim@nest.example" and t.sent[-1]["subject"].startswith("Re:")
    assert w.nudge_quiet_conversations(now=IN_HOURS.replace(day=17)) == 0   # our message is last now


def test_daily_brief_is_plain_numbers(store):
    lead, _ = store.upsert_lead(source="hn", dedupe_key="b1", company="Acme")
    store.log_event(lead["id"], "message_in", {"text": "hi", "hot": True}, actor="prospect")
    text = daily_brief(store)
    assert text.startswith("☀️ Autocrat brief") and "1 replies (1 hot)" in text


def test_folded_headers_are_flattened_before_sending(monkeypatch):
    from nexus.integrations.email_channel import EmailTransport
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, msg): captured["msg"] = msg

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    t = EmailTransport()
    t.address, t.password, t.smtp_port = "yash@causalith.com", "x", 465
    t.send("founder@saas.example", "Re: Reply from\r\n Support Team", "hi", in_reply_to="<a@b>\r\n",
           references="<one@x>\r\n\t<two@y>")
    msg = captured["msg"]
    assert msg["References"] == "<one@x> <two@y> <a@b>" and msg["Subject"] == "Re: Reply from Support Team"
