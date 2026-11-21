"""Closer logic against a fake WhatsApp Web page: no browser, no real account."""

import random
from types import SimpleNamespace

import pytest

from nexus.core.runtime import RuntimeControl
from nexus.integrations import whatsapp_closer as wc
from nexus.integrations.whatsapp_alerts import WhatsAppAlertNotifier


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def click(self):
        self.page.clicks.append(self.selector)

    def wait_for(self, **kw):
        pass

    def count(self):
        return 1

    def is_visible(self):
        return True


class FakeKeyboard:
    def __init__(self):
        self.typed, self.pressed = [], []

    def type(self, text, delay=0):
        self.typed.append(text)

    def press(self, key):
        self.pressed.append(key)

    def insert_text(self, text):
        self.typed.append(text)


class FakePage:
    def __init__(self, chats, messages):
        self.chats, self.messages = chats, messages
        self.clicks, self.keyboard = [], FakeKeyboard()

    def evaluate(self, script, arg=None):
        return self.chats if script == wc.JS_UNREAD_CHATS else self.messages

    def locator(self, selector):
        return FakeLocator(self, selector)


class FakeRouter:
    def __init__(self, data):
        self.data, self.calls = data, []

    def complete(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return SimpleNamespace(data=self.data, text="")


def make_closer(store, decision, voice="hey! yeah doable"):
    notifier = WhatsAppAlertNotifier(numbers=["+919999999999"], groups=["Founders HQ"],
                                     sender=lambda *a: None, quiet_hours="")
    sleeps = []
    closer = wc.WhatsAppCloser(notifier, store=store, router=FakeRouter(decision), voice=voice,
                               rng=random.Random(1), sleep=sleeps.append)
    return closer, notifier, sleeps


def alerts(notifier):
    out = []
    while not notifier._queue.empty():
        out.append(notifier._queue.get_nowait())
    return out


INBOUND = [{"meta": "[10:01] +971 50 123 4567: ", "text": "hi saw your demo, how much for this?", "incoming": True}]
CHATS = [{"title": "+971 50 123 4567", "unread": "1"}]


def test_humanize_strips_dashes_and_ai_tells():
    text = wc.humanize("As an AI, I can help — totally doable – about a week. Certainly! Let's go")
    assert "—" not in text and "–" not in text
    assert "As an AI" not in text and "Certainly!" not in text
    assert "totally doable, about a week" in text


def test_phone_from_title():
    assert wc.phone_from_title("+971 50 123 4567") == "+971501234567"
    assert wc.phone_from_title("Mom") == ""
    assert wc.phone_from_title("Room 101") == ""


def test_skip_rules_protect_a_personal_account(store):
    closer, notifier, _ = make_closer(store, {})
    assert closer.should_skip("Founders HQ")              # alert group
    assert closer.should_skip("+91 99999 99999")          # founder's own number
    assert closer.should_skip("Mom")                      # saved contact, not a lead
    assert not closer.should_skip("+971 50 123 4567")     # unsaved number → eligible
    store.upsert_lead(source="t", dedupe_key="whatsapp:name:acme dental", name="Acme Dental")
    assert not closer.should_skip("Acme Dental")          # saved contact that is a known lead
    closer.allow = {"rahul client"}
    assert not closer.should_skip("Rahul Client")


def test_dry_run_only_drafts(store):
    assert RuntimeControl().is_dry_run
    closer, notifier, sleeps = make_closer(store, {"reply": "hey! for that it's from $1,250 — want a quick call?",
                                                   "intent": "pricing", "hot": True, "summary": "asked price"})
    page = FakePage(CHATS, INBOUND)
    assert closer.poll(page) == 1
    assert page.keyboard.typed == []                      # nothing typed into WhatsApp
    sent = alerts(notifier)
    assert any(a["priority"] == "red" and "Hot on WhatsApp" in a["text"] for a in sent)
    draft = [a for a in sent if "[dry run]" in a["text"]][0]["text"]
    assert "—" not in draft and "$1,250" in draft
    lead = store.get_lead_by_dedupe_key("whatsapp:971501234567")
    kinds = [e["type"] for e in store.lead_events(lead["id"])]
    assert "message_in" in kinds and "draft_out" in kinds and "message_out" not in kinds


def test_live_mode_types_like_a_person(store):
    RuntimeControl().set_run_mode("live", actor="test")
    closer, notifier, sleeps = make_closer(store, {"reply": "yeah totally doable\n\nwant the demo link?",
                                                   "intent": "interested", "hot": False, "summary": "keen"})
    page = FakePage(CHATS, INBOUND)
    assert closer.poll(page) == 1
    assert page.keyboard.typed == ["yeah totally doable", "want the demo link?"]   # two separate bubbles
    assert page.keyboard.pressed.count("Enter") == 2
    assert sleeps and sleeps[1] >= 12                     # reading delay before typing
    lead = store.get_lead_by_dedupe_key("whatsapp:971501234567")
    assert [e for e in store.lead_events(lead["id"]) if e["type"] == "message_out"]


def test_stop_means_dnc_and_silence_after(store):
    RuntimeControl().set_run_mode("live", actor="test")
    closer, notifier, _ = make_closer(store, {"reply": "no worries, all the best!", "intent": "stop",
                                              "hot": False, "summary": "not interested"})
    page = FakePage(CHATS, [{"meta": "", "text": "stop messaging me", "incoming": True}])
    closer.poll(page)
    lead = store.get_lead_by_dedupe_key("whatsapp:971501234567")
    assert lead["state"] == "DNC"
    page2 = FakePage(CHATS, [{"meta": "", "text": "hello?", "incoming": True}])
    typed_before = len(page2.keyboard.typed)
    closer.poll(page2)
    assert len(page2.keyboard.typed) == typed_before      # no auto-reply to a DNC contact
    assert any("DNC contact" in a["text"] for a in alerts(notifier))


def test_no_action_when_we_spoke_last(store):
    closer, notifier, _ = make_closer(store, {"reply": "x", "intent": "other"})
    page = FakePage(CHATS, INBOUND + [{"meta": "", "text": "sent you the link", "incoming": False}])
    assert closer.poll(page) == 0
    assert closer.router.calls == []


def test_kill_switch_blocks_sending(store):
    RuntimeControl().set_run_mode("live", actor="test")
    RuntimeControl().engage_kill_switch("test")
    closer, _, _ = make_closer(store, {"reply": "hi", "intent": "question"})
    page = FakePage(CHATS, INBOUND)
    closer.poll(page)
    assert page.keyboard.typed == []


def test_rate_limit_per_chat(store):
    closer, _, _ = make_closer(store, {})
    closer.max_replies_per_hour = 2
    assert closer.rate_ok("x") and (closer._replies.setdefault("x", []).append(9e9) or True)
    closer._replies["x"] = [__import__("time").time()] * 2
    assert not closer.rate_ok("x")


def test_prompt_uses_persona_and_honesty_rule():
    prompt = wc.build_system_prompt("hey!", {}, name="Parth")
    assert "on behalf of Parth" in prompt and "Yash" not in prompt
    assert "sincerely ask" in prompt and "empty reply" in prompt
