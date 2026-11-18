"""Closer upgrades: real proof only, stronger model on money moments, no lost replies, more safe volume."""

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nexus.core.runtime import RuntimeControl
from nexus.integrations import deals, mx_check
from nexus.integrations.email_channel import EmailWorker, is_opt_out
from nexus.integrations.mailboxes import MailboxPool
from nexus.integrations.whatsapp_closer import WhatsAppCloser, build_system_prompt, closer_lint
from tests.test_conversion import IN_HOURS, RecentTransport, mail, sent_lead
from tests.test_email_channel import FakeAlerts, FakeCloser

NOW = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)          # Tue 12:00 Dubai


def live():
    RuntimeControl().set_run_mode("live", actor="test")


def worker(store, t=None, decision=None):
    t = t or RecentTransport()
    w = EmailWorker(store, FakeCloser(decision or {"reply": "sure, what's your setup today?", "intent": "question",
                                                   "hot": False}, store), FakeAlerts(), transport=t, sleep=lambda s: None)
    w.auto_send, w.daily_cap, w.hours, w.offset, w.rng = True, 20, "9-18", 240, random.Random(7)
    return w


class Router:
    """Hands out canned answers in order and records which task (model tier) each call used."""

    def __init__(self, answers, refusal=None):
        self.answers, self.tasks, self.prompts, self._refusal = list(answers), [], [], refusal

    def task_refusal(self, task):
        return self._refusal

    def complete(self, prompt, task="default", **kwargs):
        self.tasks.append(task)
        self.prompts.append(prompt)
        return SimpleNamespace(data=dict(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]))


def closer(store, router):
    return WhatsAppCloser(FakeAlerts(), store=store, router=router, voice="", sleep=lambda s: None)


# ── real proof only ──────────────────────────────────────────────

def test_lint_blocks_made_up_proof_and_payment_details_but_not_honest_replies():
    assert closer_lint("fair question. what are you using for enquiries today?") == []
    assert closer_lint("we built an ai agent marketplace end to end for a client. want to see how it'd work for you?") == []
    assert closer_lint("it's $2,500, 50% to start and the rest once it's live.") == []
    assert closer_lint("here's the room: https://meet.jit.si/windrose-abc", ["https://meet.jit.si/windrose-abc"]) == []
    bad = ["our clients love it", "we've helped 40 agencies do this", "trusted by 200 brokers",
           "10 years of experience building these", "this will increase your leads by 30%",
           "send it to account number 12345", "invoice WR-2026-0042 attached", "see https://evil.example/pay"]
    for text in bad:
        assert closer_lint(text, ["meet.jit.si"]), text


def test_brief_carries_real_proof_the_mvp_offer_and_the_terms():
    prompt = build_system_prompt("hey", {"guarantee": True, "care_plan_usd": 149.0, "referral_usd": 150.0,
                                         "site": "studio.example"}, proof="- A marketplace we built for a client.")
    assert "A marketplace we built for a client." in prompt and "live in 30 days or you don't pay" in prompt
    assert "do NOT quote a price in writing" in prompt and "Deposit-back guarantee" in prompt
    assert "$149 a month" in prompt and "$150 for an introduction" in prompt and "studio.example" in prompt
    assert "we're new" not in prompt
    assert "do not mention past work at all" in build_system_prompt("hey", {})


def test_made_up_claims_get_one_rewrite_then_the_reply_is_held(store):
    lead, _ = store.upsert_lead(source="x", dedupe_key="l1", name="Sara", channel="email", contact="s@x.example")
    fixed = Router([{"reply": "our clients see 40% more leads", "intent": "question"},
                    {"reply": "fair. what are you using today?", "intent": "question"}])
    out = closer(store, fixed).decide("Them: does it work?", lead_id=lead["id"], channel="email")
    assert out["reply"] == "fair. what are you using today?" and not out["held"] and fixed.tasks == ["closer", "closer"]
    assert "breaks these rules" in fixed.prompts[1]
    stuck = Router([{"reply": "we've helped 50 agencies", "intent": "question"}])
    out = closer(store, stuck).decide("Them: does it work?", lead_id=lead["id"], channel="email")
    assert out["reply"] == "" and out["held"] and out["draft"] == "we've helped 50 agencies"


# ── stronger model on money moments ──────────────────────────────

def test_hot_replies_use_the_stronger_model_until_its_cap(store):
    lead, _ = store.upsert_lead(source="x", dedupe_key="h1", name="Sara", channel="email", contact="s@x.example")
    answers = [{"reply": "it's 2500", "intent": "pricing", "hot": True},
               {"reply": "most setups like yours land at $2,500, half to start. what volume are we talking?",
                "intent": "pricing", "deal_usd": 2500}]
    r = Router(answers)
    out = closer(store, r).decide("Them: how much?", lead_id=lead["id"], channel="email")
    assert r.tasks == ["closer", "closer_hot"] and out["model"] == "strong" and out["reply"].startswith("most setups")
    assert out["deal_usd"] == 2500
    capped = Router(answers, refusal="closer_hot budget reached ($2.00 of $2.00)")
    out = closer(store, capped).decide("Them: how much?", lead_id=lead["id"], channel="email")
    assert capped.tasks == ["closer"] and out["model"] == "standard" and out["reply"] == "it's 2500"
    cold = Router([{"reply": "what's your setup today?", "intent": "question"}])
    closer(store, cold).decide("Them: tell me more", lead_id=lead["id"], channel="email")
    assert cold.tasks == ["closer"]


def test_task_budget_stops_the_expensive_route(store):
    from nexus.core.llm_router import BudgetExceeded
    from tests.test_llm_router import make_router
    router = make_router(store)
    router.config.set("llm", "task_budgets", {"closer_hot": 2.0})
    assert router.task_refusal("closer_hot") is None and router.task_refusal("triage") is None
    store.record_llm_usage(provider="anthropic", model="m", task="closer_hot", cost_usd=2.01)
    assert "closer_hot budget reached" in router.task_refusal("closer_hot")
    try:
        router.complete("hi", task="closer_hot")
        assert False, "should have refused"
    except BudgetExceeded:
        pass


# ── no reply is lost ─────────────────────────────────────────────

def test_opt_outs_and_empty_replies_are_never_dropped(store):
    live()
    assert is_opt_out("Re: quick one", "Unsubscribe") and is_opt_out("unsubscribe", "") and is_opt_out("x", "no thanks.")
    assert not is_opt_out("Re: quick one", "no") and not is_opt_out("Re: quick one", "no, go ahead and show me")
    t = RecentTransport()
    w = worker(store, t)
    a, _ = sent_lead(store, w, t, email="omar@palm.example", key="o1")
    b, _ = sent_lead(store, w, t, email="sara@dune.example", key="o2")
    before = len(t.sent)
    t.inbox = [mail(uid="1", **{"from": "omar@palm.example"}, message_id="<u@x>", subject="unsubscribe", body=""),
               mail(uid="2", **{"from": "sara@dune.example"}, message_id="<e@x>", body="")]
    assert w.handle_replies() == 2
    assert store.get_lead(a["id"])["state"] == "DNC" and store.get_lead(b["id"])["state"] == "REPLIED"
    assert len(t.sent) == before and w.closer.calls == []            # no AI call, nothing sent
    texts = " | ".join(text for _, text in w.alerts.items)
    assert "opted out" in texts and "no readable text" in texts
    assert not w._quiet(store.get_lead(b["id"]))                     # follow-ups stop for the empty reply too


def test_not_now_sets_a_dated_check_back(store):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="kim@nest.example", key="s1")
    real = WhatsAppCloser(FakeAlerts(), store=store, router=Router([{}]), voice="", sleep=lambda s: None)
    real.apply_outcome(lead, "try me in january", {"intent": "other", "hot": False, "follow_up_days": 30,
                                                   "phone": "+971501234567"}, alert=False)
    store.log_event(lead["id"], "message_out", {"text": "sure, i'll check back then", "channel": "email"}, actor="closer")
    assert store.get_lead(lead["id"])["data"]["call_phone"] == "+971501234567"
    before = len(t.sent)
    assert w.nudge_quiet_conversations(now=NOW + timedelta(days=5)) == 0 and len(t.sent) == before   # still snoozed
    assert w.nudge_quiet_conversations(now=NOW + timedelta(days=35)) == 1
    assert "check back around now" in t.sent[-1]["body"]
    assert w.nudge_quiet_conversations(now=NOW + timedelta(days=40)) == 0                             # once


def test_day_before_confirmation_goes_out_once(store):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="sara@dune.example", key="c1")
    start = NOW + timedelta(hours=24)
    store.log_event(lead["id"], "meeting_booked", {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "link": "https://x",
                                                   "their_offset": 240}, actor="closer")
    assert w.meeting_reminders(now=NOW) == 1 and "still good for" in t.sent[-1]["body"]
    assert w.meeting_reminders(now=NOW) == 0


# ── more safe volume ─────────────────────────────────────────────

def test_mail_server_check(store, monkeypatch):
    monkeypatch.setenv("MX_CHECK", "1")
    answers = {("good.example", "MX"): ["10 mx.good.example."], ("null.example", "MX"): ["0 ."],
               ("gone.example", "MX"): [], ("gone.example", "A"): [], ("aonly.example", "MX"): [],
               ("aonly.example", "A"): ["203.0.113.5"], ("flaky.example", "MX"): None}
    calls = []
    lookup = lambda d, r: calls.append((d, r)) or answers.get((d, r))  # noqa: E731
    ok = lambda d: mx_check.has_mail_server(d, store, lookup=lookup)  # noqa: E731
    assert ok("a@good.example") and ok("aonly.example") and ok("flaky.example")      # can't check = don't block
    assert not ok("null.example") and not ok("gone.example") and not ok("x@gmial.com")
    assert ok("someone@gmail.com") and ("gmail.com", "MX") not in calls
    n = len(calls)
    assert ok("good.example") and not ok("gone.example") and len(calls) == n          # cached


def test_dead_domains_are_never_emailed(store, monkeypatch):
    live()
    t = RecentTransport()
    w = worker(store, t)
    monkeypatch.setattr(mx_check, "has_mail_server", lambda domain, store=None, lookup=None: False)
    from tests.test_email_channel import staged_email_lead
    lead, out = staged_email_lead(store, email="x@dead.example", key="d1")
    store.update_lead_fields(lead["id"], market="UAE")
    assert w.send_openers(IN_HOURS) == 0 and t.sent == []
    assert store.get_outreach(out["id"])["status"] == "rejected" and store.get_lead(lead["id"])["state"] == "ARCHIVED"


def test_hiring_posts_and_follow_ups_jump_the_queue(store):
    w = worker(store)
    ids = {}
    for key, source, data in (("p1", "list", {}), ("p2", "forum_jobs", {"hiring": True}), ("p3", "list", {})):
        lead, _ = store.upsert_lead(source=source, dedupe_key=key, channel="email", contact=f"{key}@x.example", data=data)
        ids[key] = store.stage_outreach(lead["id"], "email", "subject: hi\nhey", kind="first_touch")
    lead, _ = store.upsert_lead(source="list", dedupe_key="p4", channel="email", contact="p4@x.example")
    ids["p4"] = store.stage_outreach(lead["id"], "email", "any thoughts?", kind="followup_1")
    order = [o["id"] for o in w.prioritise([ids["p1"], ids["p2"], ids["p3"], ids["p4"]])]
    assert order[0] == ids["p4"]["id"] and order[1] == ids["p2"]["id"]


def test_follow_up_one_offers_what_we_noticed_for_free(store):
    w = worker(store)
    lead, _ = store.upsert_lead(source="x", dedupe_key="f1", channel="email", contact="a@palm.example",
                                data={"site_check": {"url": "https://www.palm.example/", "findings": ["a", "b", "c", "d"]}})
    assert w.draft_followup(lead, 1, "opener") == ("i actually jotted down 3 quick things i noticed on palm.example "
                                                   "while i was looking at it. takes a minute to read. want me to send them over?")
    assert w.followup_template(lead, 2) == ""


def test_whatsapp_second_touch_for_silent_leads_with_a_mobile(store):
    live()
    from nexus.integrations.telegram_bot import TelegramBot
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="omar@palm.example", key="w1")
    store.update_lead_fields(lead["id"], data={**(lead["data"] or {}), "phone": "+971501234567"})
    assert w.stage_whatsapp_touch(now=NOW) == 0                                       # too soon
    with store.transaction() as c:
        c.execute("UPDATE outreach SET sent_at=? WHERE lead_id=?", (NOW.timestamp() - 6 * 86400, lead["id"]))
    assert w.stage_whatsapp_touch(now=NOW) == 1 and w.stage_whatsapp_touch(now=NOW) == 0
    bot = TelegramBot("t", allowed_user_ids=["1"], allowed_chat_ids=[], store=store)
    ready = bot.ready_whatsapp()
    assert len(ready) == 1 and ready[0]["url"].startswith("https://wa.me/971501234567?text=hi")
    note = bot.mark_whatsapp_sent(ready[0]["id"], "telegram:1")
    assert f"advance {lead['id']} to replied" in note


# ── money after the sale, and the guarantee ──────────────────────

def test_invoice_carries_the_guarantee_and_post_sale_emails_go_once(store, monkeypatch):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="sara@dune.example", key="g1")
    assert "you get this back in full" in deals.payment_email(lead, 625, "USD", "WR-2026-0001", total=1250)
    monkeypatch.setenv("DEPOSIT_GUARANTEE", "0")
    assert "back in full" not in deals.payment_email(lead, 625, "USD", "WR-2026-0001", total=1250)
    store.transition(lead["id"], "REPLIED")
    store.record_payment(lead["id"], 1250, currency="USD", kind="final", actor="test")
    assert store.get_lead(lead["id"])["state"] == "WON"
    at = lambda days: NOW.timestamp() - days * 86400  # noqa: E731
    with store.transaction() as c:
        c.execute("UPDATE payments SET ts=?", (at(8),))
    assert deals.post_sale_followups(w, now=NOW) == 1
    assert "testimonial" in t.sent[-1]["body"] and "$149 a month" in t.sent[-1]["body"]
    assert deals.post_sale_followups(w, now=NOW) == 0
    with store.transaction() as c:
        c.execute("UPDATE payments SET ts=?", (at(22),))
    assert deals.post_sale_followups(w, now=NOW) == 1 and "$150 thank-you" in t.sent[-1]["body"]
    assert deals.post_sale_followups(w, now=NOW) == 0


# ── several inboxes ──────────────────────────────────────────────

class Box:
    configured = True

    def __init__(self, address, name):
        self.address, self.from_name, self.sent, self.inbox, self.seen = address, name, [], [], []

    def send(self, to, subject, body, in_reply_to="", references="", ics=""):
        self.sent.append({"to": to, "body": body, "in_reply_to": in_reply_to})
        return f"<{len(self.sent)}.{self.address.split('@')[0]}@studio.example>"

    def fetch_recent(self, days=3, limit=40, wanted=None):
        return [dict(m) for m in self.inbox]

    def mark_seen(self, uid):
        self.seen.append(uid)

    def answered_by_me(self, message_id):
        return False


def test_threads_stay_in_their_inbox_and_new_ones_go_where_there_is_room(store):
    a, b = Box("yash@studio.example", "Yash"), Box("parth@studio.example", "Parth")
    pool = MailboxPool(store, [a, b])
    pool.primary_cap = 2
    # Until we've read an extra inbox, it starts nothing: replies we can't see are lost sales.
    assert pool.extra_capacity() == 0 and pool.pick() is a
    b.fetch_recent = lambda **kw: (_ for _ in ()).throw(RuntimeError("IMAP not enabled"))
    assert pool.fetch_recent() == [] and b.readable is False and pool.pick() is a
    del b.fetch_recent
    pool.fetch_recent()
    assert b.readable is True
    assert pool.address == "yash@studio.example" and pool.extra_capacity() == 5      # a new inbox starts at 5 a day
    first = pool.send("x@lead.example", "hi", "hey, quick one\n\nYash\nStudio")
    assert len(b.sent) == 1 and "Parth\nStudio" in b.sent[0]["body"]               # most room: the new inbox, in its own name
    pool.send("x@lead.example", "Re: hi", "any thoughts?\n\nYash", in_reply_to=first, references=first)
    assert len(b.sent) == 2 and b.sent[1]["body"].endswith("Parth")                 # the thread stays where it started
    for i in range(4):
        pool.send(f"n{i}@lead.example", "hi", "hey")
    assert len(a.sent) + len(b.sent) == 6 and len(a.sent) >= 1                       # spread by remaining room
    a.inbox = [{"uid": "7", "message_id": "<in@lead.example>", "from": "y@lead.example"}]
    got = pool.fetch_recent()
    assert got[0]["uid"] == "yash@studio.example|7" and got[0]["mailbox"] == "yash@studio.example"
    sent_before = len(a.sent)
    pool.send("y@lead.example", "Re: x", "sure\n\nYash", in_reply_to="<in@lead.example>")
    assert len(a.sent) == sent_before + 1 and a.sent[-1]["body"].endswith("Yash")    # answered from the inbox they wrote to
    pool.mark_seen(got[0]["uid"])
    assert a.seen == ["7"]
    w = worker(store, pool)
    status = w.cap_status()
    assert status["cap"] == pool.primary_cap + pool.extra_capacity() and len(status["mailboxes"]) == 2


def test_cold_lists_leave_room_for_leads_with_a_live_signal(store, monkeypatch):
    live()
    monkeypatch.setenv("EMAIL_RESERVE_PCT", "50")
    t = RecentTransport()
    w = worker(store, t)
    w.daily_cap = 4
    for i in range(4):
        lead, _ = store.upsert_lead(source="list", dedupe_key=f"row{i}", channel="email", contact=f"b{i}@x.example",
                                    market="UAE", data={"row": i})
        store.stage_outreach(lead["id"], "email", "subject: hi" + chr(10) + "hey there")
    lead, _ = store.upsert_lead(source="show_hn", dedupe_key="sig1", channel="email", contact="founder@y.example", market="UAE")
    store.stage_outreach(lead["id"], "email", "subject: hi" + chr(10) + "hey, saw your launch")
    assert w.send_openers(IN_HOURS) in (2, 3)
    sent_to = [m["to"] for m in t.sent]
    # list mail stops once the day's total reaches half the limit; the lead with a signal always gets through
    assert "founder@y.example" in sent_to and 1 <= sum(a.startswith("b") for a in sent_to) <= 2


def test_signoffs_are_stripped_and_enquiry_findings_stay_with_enquiry_businesses():
    from nexus.integrations import site_audit
    from nexus.integrations.whatsapp_closer import strip_signoff
    nl = chr(10)
    assert strip_signoff("sounds solid." + nl + nl + "yash") == "sounds solid."
    assert strip_signoff("ok, tue 4pm works?" + nl + "cheers, Yash") == "ok, tue 4pm works?"
    assert strip_signoff("thanks!" + nl + "Yash Shukla") == "thanks!"
    assert strip_signoff("yash here, quick one: does tue work?") == "yash here, quick one: does tue work?"
    result = {"findings": [{"key": "no_instant_contact", "severity": 3, "line": "no whatsapp button or live chat, so x"},
                           {"key": "heavy_js", "severity": 4, "line": "about 2 MB of javascript"}]}
    assert site_audit.top_findings(result, "vibe_coder") == ["about 2 MB of javascript"]
    assert site_audit.top_findings(result, "real_estate")[0].startswith("no whatsapp")
    assert not site_audit.relevant_line("no whatsapp button or live chat, so x", "mvp")
    assert site_audit.relevant_line("no whatsapp button or live chat, so x", "insurance")


def test_one_email_at_a_time_and_a_blocked_inbox_pauses_once(store, monkeypatch):
    live()
    from nexus.integrations import mailboxes
    monkeypatch.setenv("EMAIL_MIN_GAP_SECONDS", "300")
    monkeypatch.setenv("EMAIL_MAX_PER_TICK", "1")
    t = RecentTransport()
    w = worker(store, t)
    for i in range(3):
        lead, _ = store.upsert_lead(source="list", dedupe_key=f"pace{i}", channel="email", contact=f"p{i}@x.example",
                                    market="UAE")
        store.stage_outreach(lead["id"], "email", "subject: hi" + chr(10) + "hey there")
    assert w.send_openers(IN_HOURS) == 1 and w.send_openers(IN_HOURS) == 0       # paced: next one minutes later
    store.kv_set("email:next_send_at", "0")

    def blocked(*a, **k):
        raise Exception("(550, b'5.4.6 Unusual sending activity detected. To unblock https://mail.zoho.in/UnblockMe')")
    t.send = blocked
    assert w.send_openers(IN_HOURS) == 0
    assert mailboxes.is_paused(store, "yash@causalith.com") and w.sending_paused()
    alerts = [text for pr, text in w.alerts.items if pr == "red"]
    assert len(alerts) == 1 and "UnblockMe" in alerts[0]
    store.kv_set("email:next_send_at", "0")
    assert w.send_openers(IN_HOURS) == 0 and len([1 for pr, _ in w.alerts.items if pr == "red"]) == 1   # no retry storm


def test_postal_address_only_on_us_and_canada_mail(store):
    live()
    t = RecentTransport()
    w = worker(store, t)
    w.postal = "Somewhere 1, Test City"
    for key, market in (("pu", "US"), ("pd", "UAE"), ("pi", "India"), ("pc", "Canada")):
        lead, _ = store.upsert_lead(source="x", dedupe_key=key, channel="email", contact=f"{key}@x.example", market=market)
        store.stage_outreach(lead["id"], "email", "subject: hi" + chr(10) + "hey")
    w.hours = ""                     # any hour, so every market is in its window for this test
    w.send_openers(IN_HOURS)
    by_to = {m["to"]: m["body"] for m in t.sent}
    assert "Test City" in by_to["pu@x.example"] and "Test City" in by_to["pc@x.example"]
    assert "Test City" not in by_to["pd@x.example"] and "Test City" not in by_to["pi@x.example"]


def test_pool_pauses_only_the_blocked_inbox(store):
    from nexus.integrations.mailboxes import MailboxBlocked, MailboxPool
    a, b = Box("yash@studio.example", "Yash"), Box("parth@studio.example", "Parth")
    b.readable = True
    pool = MailboxPool(store, [a, b])
    pool.primary_cap = 1             # the new inbox has more room, so it's picked first

    def blocked(*args, **kw):
        raise Exception("550 5.4.6 Unusual sending activity detected")
    b.send = blocked
    try:
        pool.send("x@lead.example", "hi", "hey")      # the new inbox has the most room, and it's blocked
        assert False
    except MailboxBlocked as e:
        blocked_address = e.address
    assert blocked_address == "parth@studio.example"
    from nexus.integrations.mailboxes import pause
    pause(store, blocked_address, 24)
    pool.send("y@lead.example", "hi", "hey")
    assert len(a.sent) == 1 and pool.extra_capacity() == 0 and not pool.all_paused()


def test_hands_off_leads_are_recorded_but_never_auto_answered(store, make_engine):
    live()
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = sent_lead(store, w, t, email="ivan@ru.example", key="ho1")
    store.update_lead_fields(lead["id"], data={**(store.get_lead(lead["id"])["data"] or {}), "hands_off": True})
    before = len(t.sent)
    t.inbox = [mail(uid="1", **{"from": "ivan@ru.example"}, message_id="<ho@x>", body="напишите мне в телеграм")]
    assert w.handle_replies() == 1
    assert len(t.sent) == before and w.closer.calls == []
    assert "handling this one by hand" in w.alerts.items[-1][1] and store.get_lead(lead["id"])["state"] == "REPLIED"
    engine = make_engine()
    out = engine.execute(f"auto {lead['id']}")
    assert out["success"] and store.get_lead(lead["id"])["data"]["hands_off"] is False


def test_example_and_help_desk_addresses_are_never_cold_emailed(store):
    live()
    from nexus.integrations.mx_check import is_unusable_address
    assert is_unusable_address("you@company.com") and is_unusable_address("jane@company.com")
    assert is_unusable_address("support@zanta.example") and is_unusable_address("legal@murer.example")
    assert not is_unusable_address("hello@palm.example") and not is_unusable_address("omar@palm.example")
    t = RecentTransport()
    w = worker(store, t)
    lead, _ = store.upsert_lead(source="show_hn", dedupe_key="fake1", channel="email", contact="you@company.com", market="UAE")
    out = store.stage_outreach(lead["id"], "email", "subject: hi" + chr(10) + "hey")
    assert w.send_openers(IN_HOURS) == 0 and store.get_outreach(out["id"])["status"] == "rejected"


def test_paused_auto_replies_become_one_tap_drafts(store):
    live()
    t = RecentTransport()
    w = worker(store, t)
    w.auto_reply = False
    lead, _ = sent_lead(store, w, t, email="ivan@ru.example", key="dr1")
    before = len(t.sent)
    t.inbox = [mail(uid="1", **{"from": "ivan@ru.example"}, message_id="<q@x>", body="how much?")]
    w.handle_replies()
    assert len(t.sent) == before and "DRAFT, not sent" in w.alerts.items[-1][1]
    assert w.alerts.buttons[0][0]["callback_data"] == f"rs:{lead['id']}"
    assert w.send_reply_draft(lead["id"]).startswith("📤 Sent")
    assert t.sent[-1]["in_reply_to"] == "<q@x>" and "what's your setup today" in t.sent[-1]["body"]
    assert w.send_reply_draft(lead["id"]).startswith(("Something was already sent", "This draft was already"))


def test_email_drafts_just_inform_when_autocrat_sends_itself(store, monkeypatch):
    from nexus.integrations.telegram_alerts import build_alert
    from tests.test_email_channel import staged_email_lead
    lead, out = staged_email_lead(store, email="omar@palm.example", key="inf1")
    entry = {"event": "lead.transition", "data": {"to": "STAGED", "lead_id": lead["id"], "payload": {"outreach_id": out["id"]}}}
    monkeypatch.setenv("EMAIL_AUTO_SEND", "1")
    alert = build_alert(entry, store=store)
    assert alert["text"].startswith("🟢 Going out automatically")
    assert [b["callback_data"] for row in alert["buttons"] for b in row if "callback_data" in b] == [f"dnc:{lead['id']}"]
    monkeypatch.setenv("EMAIL_AUTO_SEND", "0")
    assert build_alert(entry, store=store)["buttons"][0][0]["callback_data"] == f"oa:{out['id']}"
