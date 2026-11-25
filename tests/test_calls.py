"""Call desk: the right numbers at the right time, an opener, and one tap that does the follow-through."""

from datetime import datetime, timedelta, timezone

from nexus.core.runtime import RuntimeControl
from nexus.integrations import calls
from nexus.integrations.email_channel import EmailWorker
from tests.test_conversion import RecentTransport
from tests.test_email_channel import FakeAlerts, FakeCloser

US_MORNING = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)    # Tue 10:00 New York, 19:00 Dubai
US_NIGHT = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)       # Mon 22:00 New York


def worker(store):
    w = EmailWorker(store, FakeCloser({"reply": "", "intent": "other"}, store), FakeAlerts(), transport=RecentTransport(),
                    sleep=lambda s: None)
    w.hours, w.offset = "9-18", 240
    return w


def phone_lead(store, key, market="US East", phone="+12025550111", email="", state="STAGED", **data):
    lead, _ = store.upsert_lead(source="insurance_agencies", dedupe_key=key, name="Acme Insurance", company="Acme Insurance",
                                channel="email" if email else "call", contact=email or phone, market=market,
                                niche="insurance", data={"call_phone": phone, "city": "Tampa", **data})
    store.transition(lead["id"], "TRIAGED")
    if state != "TRIAGED":
        out = store.stage_outreach(lead["id"], "email" if email else "call",
                                   "hi, is this the owner? quick one about quote requests" if not email else "subject: hi\nhey")
        store.transition(lead["id"], "STAGED", payload={"outreach_id": out["id"]})
    return store.get_lead(lead["id"])


def test_e164():
    assert calls.e164("(202) 555-0111", "1") == "+12025550111"
    assert calls.e164("1-202-555-0111", "1") == "+12025550111"
    assert calls.e164("+44 20 7946 0000") == "+442079460000"
    assert calls.e164("050 123 4567", "971") == "+971501234567"
    assert calls.e164("n/a", "1") == "" and calls.e164("", "1") == ""


def test_sheet_lists_us_numbers_in_their_hours_only(store):
    us = phone_lead(store, "a", reviews="212", rating="4.8")
    phone_lead(store, "b", market="UAE", phone="+971501234567")                     # cold UAE: never cold-called
    dnc = phone_lead(store, "c", phone="+12025550133")
    store.transition(dnc["id"], "DNC", reason="test")
    sheet = calls.call_sheet(store, now=US_MORNING)
    assert [s["lead"]["id"] for s in sheet] == [us["id"]]
    assert sheet[0]["local_time"] == "10:00 am" and "quote requests" in sheet[0]["opener"]
    card = calls.sheet_card(sheet[0], 1)
    assert "+12025550111" in card and "⭐4.8 (212)" in card and "Tampa" in card
    assert calls.call_sheet(store, now=US_NIGHT) == []                             # nobody rung at 10pm their time
    assert len(calls.call_sheet(store, now=US_NIGHT, ignore_hours=True)) == 1


def test_hot_reply_from_any_market_goes_on_top(store):
    phone_lead(store, "a")
    hot = phone_lead(store, "h", market="UAE", phone="+971501234567", email="omar@palm.example", state="STAGED")
    out = store.outreach_for_lead(hot["id"])[0]
    store.approve_outreach(out["id"], actor="t")
    store.transition(hot["id"], "APPROVED")
    store.mark_outreach_sent(out["id"], actor="t")
    store.transition(hot["id"], "REPLIED")
    store.log_event(hot["id"], "message_in", {"text": "how much?", "hot": True}, actor="prospect")
    sheet = calls.call_sheet(store, now=datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc))   # 11:00 Dubai, 2am NY
    assert [s["lead"]["id"] for s in sheet] == [hot["id"]] and sheet[0]["hot"]


def test_outcomes_follow_through(store, monkeypatch):
    RuntimeControl().set_run_mode("live", actor="test")
    monkeypatch.setenv("OWNER_AVAILABILITY", "Mon-Sat 10-22")
    w = worker(store)
    na = phone_lead(store, "n", phone="+12025550144")
    assert "no answer (1/3)" in calls.log_call(store, w, na["id"], "na", now=US_MORNING)
    assert store.get_lead(na["id"])["state"] == "SENT"          # the staged call opener counts as the first touch
    with store.transaction() as c:
        c.execute("UPDATE lead_events SET ts=? WHERE type='call'", (US_MORNING.timestamp(),))
    assert all(s["lead"]["id"] != na["id"] for s in calls.call_sheet(store, now=US_MORNING))     # not again today
    tomorrow = [s for s in calls.call_sheet(store, now=US_MORNING + timedelta(days=1)) if s["lead"]["id"] == na["id"]]
    assert tomorrow and tomorrow[0]["tries"] == 1

    yes = phone_lead(store, "y", phone="+12025550155")
    note = calls.log_call(store, w, yes["id"], "yes", now=US_MORNING)
    assert "interested" in note and f"email {yes['id']}" in note           # no email yet: asks you for it
    assert store.get_lead(yes["id"])["state"] == "REPLIED"
    note = calls.set_email(store, w, yes["id"], "Owner@Acme.example", actor="t")
    sent = w.transport.sent[-1]
    assert "Follow-up with two times sent" in note and sent["to"] == "owner@acme.example"
    assert "great talking just now" in sent["body"] and " or " in sent["body"]
    assert "Saved" in calls.set_email(store, w, yes["id"], "owner@acme.example", actor="t")   # never twice

    no = phone_lead(store, "x", phone="+12025550166")
    calls.log_call(store, w, no["id"], "no", now=US_MORNING)
    assert store.get_lead(no["id"])["state"] == "DNC" and store.is_suppressed("phone:+12025550166")
