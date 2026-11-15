from datetime import datetime, timezone

import pytest

from nexus.core.events import EventBus
from nexus.integrations.whatsapp_alerts import (
    RecipientNotAllowed, WhatsAppAlertNotifier, to_whatsapp_text,
)


def make(sent, **kw):
    kw.setdefault("quiet_hours", "")
    return WhatsAppAlertNotifier(numbers=["+91 98765 43210"], groups=["Founders HQ"],
                                 sender=lambda kind, target, text: sent.append((kind, target, text)), **kw)


def test_only_allowlisted_recipients():
    sent = []
    n = make(sent)
    n.send("number", "+919876543210", "hi")
    n.send("group", "Founders HQ", "hi")
    with pytest.raises(RecipientNotAllowed):
        n.send("number", "+971500000001", "prospect")       # a prospect's number
    with pytest.raises(RecipientNotAllowed):
        n.send("group", "Some Other Group", "x")
    assert [s[1] for s in sent] == ["+919876543210", "Founders HQ"]


def test_html_to_whatsapp_formatting():
    text = to_whatsapp_text("🔴 Demo <b>FORWARDED</b>\n<i>note</i> &amp; <code>abc123</code>")
    assert text == "🔴 Demo *FORWARDED*\n_note_ & abc123"


def test_batch_merges_alerts_and_holds_orange_in_quiet_hours(monkeypatch):
    sent = []
    n = make(sent, quiet_hours="23-8")
    import nexus.integrations.whatsapp_alerts as mod
    monkeypatch.setattr(mod, "in_quiet_hours", lambda spec: True)
    msg = n.compose_batch([{"text": "orange one", "priority": "orange"},
                           {"text": "RED one", "priority": "red"}])
    assert "RED one" in msg and "orange one" not in msg
    monkeypatch.setattr(mod, "in_quiet_hours", lambda spec: False)
    morning = n.compose_batch([])
    assert "Held overnight (1)" in morning and "orange one" in morning


def test_rate_limit_holds_extra_messages():
    sent = []
    n = make(sent, max_per_hour=2)
    for i in range(3):
        n._flush([{"text": f"alert {i}", "priority": "red"}])
    assert len(sent) == 4            # 2 messages × 2 recipients
    assert n._held and "alert 2" in n._held[0]


def test_events_flow_to_notifier_queue():
    sent = []
    n = make(sent)
    n.attach()
    EventBus().emit("control.kill_switch", {"engaged": True, "actor": "telegram:1", "reason": "test"})
    item = n._queue.get_nowait()
    assert item["priority"] == "red" and "Kill switch" in item["text"] and "<b>" not in item["text"]


def test_not_started_without_recipients():
    n = WhatsAppAlertNotifier(numbers=[], groups=[], sender=lambda *a: None)
    assert n.start() is False
