"""
Autocrat — WhatsApp through Meta's official Cloud API (inbound closer, no WhatsApp Web).

How it stays free and allowed
  * We never start a WhatsApp conversation. Prospects tap the "WhatsApp me" link in our emails
    (or message the number themselves); that opens a 24-hour customer-service window, and
    free-form replies inside it cost nothing.
  * Replies go out only inside that window. Outside it Meta would require a paid, pre-approved
    template; we just alert you instead.
  * "stop" / "not interested" → DNC forever, same as email.

Flow
  Meta → POST /webhooks/whatsapp (X-Hub-Signature-256 checked with the app secret)
       → WhatsAppCloud.handle_payload (dedupes Meta's retries, runs on a worker thread)
       → lead found by phone or by the email "ref" code, else a new inbound lead
       → closer brain (playbook + voice + lead context) → reply via Graph API
       → one alert to Telegram with what they said and what we answered.

Settings (.env): WHATSAPP_CLOUD_ENABLED, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN,
WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, WHATSAPP_BUSINESS_NUMBER, WHATSAPP_GRAPH_VERSION.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from nexus.core.lead_state import contact_key
from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import get_secret

log = get_logger("whatsapp_cloud")

WINDOW_S = 24 * 3600
REF_RE = re.compile(r"\bref[:\s#-]*([0-9a-f]{6,12})\b", re.I)


def verify_signature(raw_body: bytes, header: str, app_secret: str) -> bool:
    """Meta signs every webhook body: X-Hub-Signature-256: sha256=<hex hmac>. Fails closed."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip())


def click_to_chat(number: str, text: str = "") -> str:
    """wa.me link for emails. The prefilled text carries a ref so we can match the chat to the lead."""
    from urllib.parse import quote
    digits = re.sub(r"\D", "", number or "")
    return f"https://wa.me/{digits}" + (f"?text={quote(text)}" if text else "") if digits else ""


def ref_for(lead_id: str) -> str:
    return (lead_id or "")[:6]


class GraphClient:
    """The two Graph API calls we need. Free-form text only (inside the 24h window)."""

    def __init__(self, phone_number_id: str, token: str, version: str = "v23.0",
                 post: Optional[Callable[..., Any]] = None):
        self.phone_number_id = phone_number_id
        self.token = token
        self.version = version
        self._post = post

    @property
    def configured(self) -> bool:
        return bool(self.phone_number_id and self.token)

    def _call(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://graph.facebook.com/{self.version}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self._post:
            return self._post(url, body, headers)
        import httpx
        resp = httpx.post(url, json=body, headers=headers, timeout=30)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            raise RuntimeError(f"Graph API {resp.status_code}: {err.get('message') or resp.text[:200]}")
        return data

    def send_text(self, to: str, text: str, reply_to: str = "") -> str:
        body: Dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual",
                                "to": re.sub(r"\D", "", to), "type": "text",
                                "text": {"preview_url": False, "body": text[:4096]}}
        if reply_to:
            body["context"] = {"message_id": reply_to}
        data = self._call(body)
        return ((data.get("messages") or [{}])[0]).get("id", "")

    def mark_read(self, message_id: str) -> None:
        self._call({"messaging_product": "whatsapp", "status": "read", "message_id": message_id})


class WhatsAppCloud:
    def __init__(self, store, closer, alerts, client: Optional[GraphClient] = None,
                 sleep: Callable[[float], None] = time.sleep, threaded: bool = True):
        self.store = store
        self.closer = closer          # WhatsAppCloser instance: the shared sales brain (decide/apply_outcome)
        self.alerts = alerts          # AlertFanout (Telegram)
        self.client = client or GraphClient(get_secret("WHATSAPP_PHONE_NUMBER_ID"), get_secret("WHATSAPP_ACCESS_TOKEN"),
                                            get_secret("WHATSAPP_GRAPH_VERSION", "v23.0") or "v23.0")
        self.app_secret = get_secret("WHATSAPP_APP_SECRET")
        self.verify_token = get_secret("WHATSAPP_VERIFY_TOKEN")
        self.sleep = sleep
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.threaded = threaded

    # ── webhook plumbing ─────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return self.client.configured and bool(self.app_secret and self.verify_token)

    def verify_challenge(self, mode: str, token: str, challenge: str) -> Optional[str]:
        if mode == "subscribe" and self.verify_token and hmac.compare_digest(token or "", self.verify_token):
            return challenge
        return None

    def accept(self, raw_body: bytes, signature: str) -> bool:
        """Called by the HTTP endpoint: check the signature, queue the work, return fast."""
        if not verify_signature(raw_body, signature, self.app_secret):
            log.warning("WhatsApp webhook with a bad signature: ignored")
            return False
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return False
        if self.threaded:
            self._ensure_worker()
            self._queue.put(payload)
        else:
            self.handle_payload(payload)
        return True

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="whatsapp-cloud")
        self._thread.start()

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                self.handle_payload(payload)
            except Exception as e:
                log.warning(f"WhatsApp webhook handling failed: {e}")

    # ── the work ─────────────────────────────────────────────────

    @staticmethod
    def messages_in(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                names = {c.get("wa_id"): ((c.get("profile") or {}).get("name") or "") for c in value.get("contacts") or []}
                for m in value.get("messages") or []:
                    text = ""
                    if m.get("type") == "text":
                        text = (m.get("text") or {}).get("body") or ""
                    elif m.get("type") == "button":
                        text = (m.get("button") or {}).get("text") or ""
                    elif m.get("type") == "interactive":
                        inter = m.get("interactive") or {}
                        text = ((inter.get("button_reply") or inter.get("list_reply") or {}).get("title")) or ""
                    out.append({"id": m.get("id", ""), "from": m.get("from", ""), "ts": float(m.get("timestamp") or time.time()),
                                "type": m.get("type", ""), "text": text, "name": names.get(m.get("from"), "")})
        return out

    def lead_for(self, wa_id: str, name: str, text: str) -> Dict[str, Any]:
        """Same person as an email/list lead? Match by phone, then by the ref in the prefilled text."""
        key = contact_key("whatsapp", "+" + wa_id)
        lead = self.store.find_lead_by_contact(key)
        if lead:
            return lead
        m = REF_RE.search(text or "")
        if m:
            row = self.store._conn().execute("SELECT id FROM leads WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
                                             (m.group(1).lower() + "%",)).fetchone()
            if row:
                origin = self.store.get_lead(row[0])
                self.store.log_event(origin["id"], "whatsapp_linked", {"wa_id": wa_id, "name": name}, actor="whatsapp")
                data = dict(origin.get("data") or {}, whatsapp=f"+{wa_id}")
                self.store.update_lead_fields(origin["id"], data=data)
                return self.store.get_lead(origin["id"])
        lead, _ = self.store.upsert_lead(source="whatsapp_inbound", dedupe_key=f"whatsapp:{wa_id}", name=name,
                                         channel="whatsapp", contact="+" + wa_id, actor="whatsapp")
        return lead

    def transcript_for(self, lead_id: str, latest: str) -> str:
        lines = []
        for e in self.store.lead_events(lead_id):
            p = e.get("payload") or {}
            if e["type"] == "message_out" and p.get("text"):
                lines.append(f"Me: {p['text']}")
            elif e["type"] == "message_in" and p.get("text"):
                lines.append(f"Them: {p['text']}")
        lines.append(f"Them: {latest}")
        return "\n\n".join(lines[-12:])

    def handle_payload(self, payload: Dict[str, Any]) -> int:
        handled = 0
        for msg in self.messages_in(payload):
            seen_key = f"wa_cloud:seen:{msg['id']}"
            if not msg["id"] or self.store.kv_get(seen_key):
                continue                      # Meta retries webhooks: handle each message once
            self.store.kv_set(seen_key, "1")
            handled += self.handle_message(msg)
        return handled

    def handle_message(self, msg: Dict[str, Any]) -> int:
        lead = self.lead_for(msg["from"], msg["name"], msg["text"])
        who = lead.get("company") or lead.get("name") or msg["name"] or f"+{msg['from']}"
        if not msg["text"]:
            self.store.log_event(lead["id"], "message_in", {"text": f"[{msg['type']}]", "channel": "whatsapp"},
                                 actor="prospect")
            self.alerts.notify(f"📲 WhatsApp from {who}: sent a {msg['type']} (not text). Your move.", "orange")
            return 1
        if lead["state"] == "DNC":
            self.alerts.notify(f"🟠 DNC contact {who} messaged on WhatsApp; not auto-replying.", "orange")
            return 1
        try:
            self.client.mark_read(msg["id"])
        except Exception as e:
            log.info(f"mark_read failed: {e}")
        decision = self.closer.decide(self.transcript_for(lead["id"], msg["text"]), lead_id=lead["id"], channel="WhatsApp")
        self.closer.apply_outcome(lead, msg["text"], decision, alert=False)
        reply = decision.get("reply") or ""
        in_window = time.time() - msg["ts"] < WINDOW_S - 300
        rt = RuntimeControl()
        if reply and rt.is_dry_run:
            outcome = f"[dry run] would answer:\n{reply}"
        elif reply and not rt.killed and in_window:
            self.sleep(min(45.0, 6 + len(msg["text"]) / 20))     # a human doesn't answer in 1 second
            try:
                wamid = self.client.send_text(msg["from"], reply, reply_to=msg["id"])
                self.store.log_event(lead["id"], "message_out", {"text": reply, "channel": "whatsapp", "wamid": wamid,
                                                                 "intent": decision.get("intent")}, actor="closer")
                outcome = f"auto-replied:\n{reply}"
            except Exception as e:
                outcome = f"reply FAILED ({e}); answer them yourself:\n{reply}"
        elif reply and not in_window:
            outcome = f"24h window closed, nothing sent (would need a paid template). Suggested reply:\n{reply}"
        elif decision.get("intent") in ("stop", "not_interested"):
            outcome = "marked do-not-contact"
        else:
            outcome = "no auto-reply: your move"
        head = "🔴 HOT WhatsApp" if decision.get("hot") else "📲 WhatsApp"
        said = " ".join(msg["text"].split())[:400]
        self.alerts.notify(f"{head} from {who} · {decision.get('intent') or 'other'}\n“{said}”\n↳ {outcome[:600]}",
                           "red" if decision.get("hot") else "orange")
        return 1
