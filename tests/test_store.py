import threading

import pytest

from nexus.core.events import EventBus
from nexus.core.lead_state import InvalidTransition, allowed_targets, contact_key, LeadState
from nexus.core.store import AlreadyContacted, Suppressed


def new_lead(store, contact="+971 50 123 4567", key="maps:clinic-1", channel="whatsapp"):
    lead, created = store.upsert_lead(source="test", dedupe_key=key, name="Clinic", channel=channel,
                                      contact=contact, market="UAE")
    return lead


def walk_to(store, lead_id, *states):
    for state in states:
        store.transition(lead_id, state)


def test_wal_mode_and_migrations(store):
    assert store.journal_mode().lower() == "wal"
    assert store.migrate() == 2


def test_upsert_dedupes(store):
    first, created = store.upsert_lead(source="hn", dedupe_key="hn:123", name="A")
    again, created_again = store.upsert_lead(source="hn", dedupe_key="hn:123", name="A (dup)")
    assert created and not created_again
    assert first["id"] == again["id"]
    assert len(store.list_leads()) == 1


def test_happy_path_and_history(store):
    lead = new_lead(store)
    walk_to(store, lead["id"], "TRIAGED", "BUILDING")
    out = store.stage_outreach(lead["id"], "whatsapp", "hello")
    walk_to(store, lead["id"], "STAGED", "APPROVED")
    store.approve_outreach(out["id"], actor="telegram:1:Yash")
    store.mark_outreach_sent(out["id"], actor="telegram:1:Yash")
    assert store.get_lead(lead["id"])["state"] == "SENT"
    walk_to(store, lead["id"], "OPENED", "FORWARDED", "REPLIED", "DEPOSIT_OPENED", "WON")
    states = [e["to_state"] for e in store.lead_events(lead["id"])]
    assert states == ["DISCOVERED", "TRIAGED", "BUILDING", "STAGED", "APPROVED", "SENT",
                      "OPENED", "FORWARDED", "REPLIED", "DEPOSIT_OPENED", "WON"]


def test_illegal_transitions_rejected(store):
    lead = new_lead(store)
    with pytest.raises(InvalidTransition):
        store.transition(lead["id"], "SENT")          # skip ahead
    with pytest.raises(InvalidTransition):
        store.transition(lead["id"], "WON")
    walk_to(store, lead["id"], "TRIAGED", "BUILDING", "STAGED", "APPROVED")
    with pytest.raises(InvalidTransition):
        store.transition(lead["id"], "SENT")          # SENT needs a real sent outreach


def test_out_of_order_telemetry_is_a_signal(store):
    lead = new_lead(store)
    walk_to(store, lead["id"], "TRIAGED", "BUILDING")
    out = store.stage_outreach(lead["id"], "whatsapp", "hi")
    walk_to(store, lead["id"], "STAGED", "APPROVED")
    store.approve_outreach(out["id"], "a")
    store.mark_outreach_sent(out["id"], "a")
    walk_to(store, lead["id"], "REPLIED")
    late = store.transition(lead["id"], "OPENED")       # beacon arrives late
    assert late["kind"] == "signal" and not late["changed"]
    assert store.get_lead(lead["id"])["state"] == "REPLIED"


def test_dnc_is_permanent_and_suppresses(store):
    lead = new_lead(store)
    store.transition(lead["id"], "DNC", reason="asked to stop")
    with pytest.raises(InvalidTransition):
        store.transition(lead["id"], "TRIAGED")
    assert store.is_suppressed(contact_key("whatsapp", "+971501234567"))
    other = new_lead(store, contact="+971-50-123-4567", key="maps:clinic-dup")
    with pytest.raises(Suppressed):
        store.stage_outreach(other["id"], "whatsapp", "hi again")


def test_stage_is_idempotent_per_lead(store):
    lead = new_lead(store)
    a = store.stage_outreach(lead["id"], "whatsapp", "first draft")
    b = store.stage_outreach(lead["id"], "whatsapp", "second draft")
    assert a["id"] == b["id"]


def test_never_messaged_twice_same_lead(store):
    lead = new_lead(store)
    out = store.stage_outreach(lead["id"], "whatsapp", "hi")
    store.approve_outreach(out["id"], "a")
    store.mark_outreach_sent(out["id"], "a")
    with pytest.raises(AlreadyContacted):
        store.mark_outreach_sent(out["id"], "a")


def test_never_messaged_twice_same_contact_across_leads(store):
    first = new_lead(store, key="maps:one")
    out = store.stage_outreach(first["id"], "whatsapp", "hi")
    store.approve_outreach(out["id"], "a")
    store.mark_outreach_sent(out["id"], "a")
    # Same phone number found again by a different source → different lead
    second = new_lead(store, contact="00971501234567", key="shopify:two")
    with pytest.raises(AlreadyContacted):
        store.stage_outreach(second["id"], "whatsapp", "hi again")


def test_concurrent_mark_sent_only_one_wins(store):
    lead = new_lead(store)
    out = store.stage_outreach(lead["id"], "whatsapp", "hi")
    store.approve_outreach(out["id"], "a")
    wins, losses = [], []

    def worker():
        try:
            store.mark_outreach_sent(out["id"], "thread")
            wins.append(1)
        except AlreadyContacted:
            losses.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(losses) == 7


def test_transitions_emit_events(store):
    seen = []
    EventBus().on("lead.*", lambda entry: seen.append(entry["event"]))
    lead = new_lead(store)
    store.transition(lead["id"], "TRIAGED")
    assert seen == ["lead.created", "lead.transition"]


def test_contact_key_normalisation():
    assert contact_key("whatsapp", "+971 (50) 123-4567") == "phone:+971501234567"
    assert contact_key("x", "@FounderName") == "x:foundername"
    assert contact_key("reddit", "/u/SomeOne") == "reddit:someone"
    assert LeadState.DNC in allowed_targets(LeadState.SENT)
    assert allowed_targets(LeadState.WON) == frozenset()


def test_budget_ledger(store):
    store.record_llm_usage(provider="gemini", model="m", task="triage", input_tokens=1000,
                           output_tokens=200, cost_usd=0.01)
    spend = store.month_spend()
    assert spend["calls"] == 1 and spend["input_tokens"] == 1000 and spend["cost_usd"] == 0.01
