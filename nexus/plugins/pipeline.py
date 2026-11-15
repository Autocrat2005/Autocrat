"""
Autocrat — Pipeline plugin (built-in, runs on core and desk).

Generic lead-pipeline commands over the WAL store. The revenue-specific
workers (radar, triage, forge, outreach) live in the private
autocrat-revenue-plugins repo and drive leads through these same calls.

First-touch sends are human-assisted: `outreach_approve` records who approved
and returns one-tap compose links; a human sends; `outreach_mark_sent` records it.
The store refuses to mark the same lead or contact as messaged twice.
"""

import json
from typing import Any, Dict

from nexus.core.context import current_actor
from nexus.core.deeplinks import compose_links
from nexus.core.lead_state import LeadState
from nexus.core.plugin import NexusPlugin
from nexus.core.runtime import RuntimeControl
from nexus.core.store import AlreadyContacted, InvalidTransition, Suppressed, get_store


class PipelinePlugin(NexusPlugin):
    name = "pipeline"
    description = "Lead pipeline: leads, state machine, staged outreach"
    version = "1.0.0"
    icon = "🛰️"
    profiles = ("core", "desk")

    def setup(self):
        self.register_command("lead_add", self.lead_add, "Add a lead (deduplicated)",
                              "lead add <name> via <channel> <contact>", side_effect="internal")
        self.register_command("lead_list", self.lead_list, "List leads, optionally by state",
                              "leads [state]", side_effect="none",
                              patterns=[r"^leads(?:\s+(?P<state>[a-z_]+))?$"])
        self.register_command("lead_show", self.lead_show, "Show a lead with its history",
                              "lead <id>", side_effect="none",
                              patterns=[r"^lead\s+(?P<lead_id>[0-9a-f]{12})$"])
        self.register_command("lead_advance", self.lead_advance, "Move a lead to a new state",
                              "advance <id> to <state>", side_effect="internal",
                              patterns=[r"^advance\s+(?P<lead_id>[0-9a-f]{12})\s+to\s+(?P<state>[a-z_]+)$"])
        self.register_command("lead_dnc", self.lead_dnc, "Mark a lead do-not-contact (permanent)",
                              "dnc <id>", side_effect="internal",
                              patterns=[r"^dnc\s+(?P<lead_id>[0-9a-f]{12})$"])
        self.register_command("outreach_stage", self.outreach_stage, "Stage a first-touch draft",
                              "stage <id> <draft>", side_effect="internal")
        self.register_command("outreach_approve", self.outreach_approve,
                              "Approve a staged draft and get one-tap send links",
                              "approve outreach <id>", side_effect="internal",
                              patterns=[r"^approve\s+outreach\s+(?P<outreach_id>[0-9a-f]{12})$"])
        self.register_command("outreach_mark_sent", self.outreach_mark_sent,
                              "Record that a human sent the approved draft",
                              "sent <outreach id>", side_effect="internal",
                              patterns=[r"^sent\s+(?P<outreach_id>[0-9a-f]{12})$"])
        self.register_command("pipeline_status", self.pipeline_status, "Counts by state + run mode",
                              "pipeline", side_effect="none",
                              patterns=[r"^pipeline(?:\s+status)?$"],
                              phrases=["how is the pipeline", "pipeline status", "how many leads"])
        self.register_command("simulate", self.simulate,
                              "Dry-run only: walk a synthetic lead DISCOVERED → STAGED",
                              "simulate lead", side_effect="internal",
                              patterns=[r"^simulate(?:\s+lead)?$"])

    # ── helpers ──────────────────────────────────────────────────

    @property
    def store(self):
        engine = getattr(self, "_engine", None)
        return engine.store if engine is not None else get_store()

    @staticmethod
    def _err(e: Exception) -> Dict[str, Any]:
        return {"success": False, "error": str(e), "error_type": type(e).__name__}

    # ── commands ─────────────────────────────────────────────────

    def lead_add(self, source: str = "manual", name: str = "", channel: str = "", contact: str = "",
                 market: str = "", niche: str = "", url: str = "", dedupe_key: str = "",
                 company: str = "", **kwargs):
        key = dedupe_key or f"{source}:{channel}:{contact or name}".lower()
        lead, created = self.store.upsert_lead(source=source, dedupe_key=key, name=name, company=company,
                                               channel=channel, contact=contact, market=market,
                                               niche=niche, url=url, actor="pipeline")
        verb = "Added" if created else "Already known"
        return {"success": True, "result": f"{verb}: {lead['id']} ({lead['state']})",
                "lead": lead, "created": created}

    def lead_list(self, state: str = "", limit: int = 25, **kwargs):
        try:
            leads = self.store.list_leads(state=state.upper() if state else None, limit=int(limit))
        except ValueError as e:
            return self._err(e)
        rows = [f"{l['id']}  {l['state']:<15} {l['name'] or l['contact'] or '-'}" for l in leads]
        return {"success": True, "result": "\n".join(rows) or "No leads yet.", "leads": leads}

    def lead_show(self, lead_id: str = "", **kwargs):
        lead = self.store.get_lead(str(lead_id))
        if not lead:
            return {"success": False, "error": f"Unknown lead {lead_id}"}
        return {"success": True, "result": json.dumps(lead, indent=2, default=str), "lead": lead,
                "events": self.store.lead_events(lead["id"]),
                "outreach": self.store.outreach_for_lead(lead["id"])}

    def lead_advance(self, lead_id: str = "", state: str = "", reason: str = "", actor: str = "",
                     **kwargs):
        actor = actor or current_actor()
        try:
            result = self.store.transition(str(lead_id), state.upper(), actor=actor, reason=reason)
        except (InvalidTransition, KeyError, ValueError) as e:
            return self._err(e)
        return {"success": True, "result": result["description"], "transition": result}

    def lead_dnc(self, lead_id: str = "", reason: str = "requested", **kwargs):
        return self.lead_advance(lead_id=lead_id, state="DNC", reason=reason)

    def outreach_stage(self, lead_id: str = "", draft: str = "", channel: str = "", **kwargs):
        lead = self.store.get_lead(str(lead_id))
        if not lead:
            return {"success": False, "error": f"Unknown lead {lead_id}"}
        try:
            out = self.store.stage_outreach(lead["id"], channel or lead["channel"], draft, actor="pipeline")
        except (AlreadyContacted, Suppressed, ValueError) as e:
            return self._err(e)
        return {"success": True, "result": f"Staged outreach {out['id']}", "outreach": out}

    def outreach_approve(self, outreach_id: str = "", actor: str = "", **kwargs):
        actor = actor or current_actor()
        out = self.store.get_outreach(str(outreach_id))
        if not out:
            return {"success": False, "error": f"Unknown outreach {outreach_id}"}
        lead = self.store.get_lead(out["lead_id"])
        try:
            out = self.store.approve_outreach(out["id"], actor=actor)
            if lead["state"] == LeadState.STAGED.value:
                self.store.transition(lead["id"], "APPROVED", actor=actor,
                                      payload={"outreach_id": out["id"]})
        except (AlreadyContacted, InvalidTransition) as e:
            return self._err(e)
        links = compose_links(out["channel"], lead.get("contact") or "", out["draft"])
        return {"success": True, "result": f"Approved {out['id']} — send it, then: sent {out['id']}",
                "outreach": out, "links": links}

    def outreach_mark_sent(self, outreach_id: str = "", actor: str = "", **kwargs):
        actor = actor or current_actor()
        try:
            out = self.store.mark_outreach_sent(str(outreach_id), actor=actor)
        except (AlreadyContacted, Suppressed, KeyError) as e:
            return self._err(e)
        return {"success": True, "result": f"Recorded as sent: {out['id']}", "outreach": out}

    def pipeline_status(self, **kwargs):
        counts: Dict[str, int] = {}
        for lead in self.store.list_leads(limit=10_000):
            counts[lead["state"]] = counts.get(lead["state"], 0) + 1
        status = RuntimeControl().status()
        lines = [f"Mode: {status['run_mode']}  Kill switch: {'ENGAGED' if status['kill_switch'] else 'off'}"]
        lines += [f"{state:<15} {n}" for state, n in sorted(counts.items())]
        return {"success": True, "result": "\n".join(lines), "counts": counts, **status}

    def simulate(self, contact: str = "+971500000000", **kwargs):
        """Synthetic end-to-end walk used to verify the pipeline in dry-run."""
        if not RuntimeControl().is_dry_run:
            return {"success": False, "error": "simulate only runs in RUN_MODE=dry_run"}
        import uuid
        tag = uuid.uuid4().hex[:6]
        lead, _ = self.store.upsert_lead(
            source="simulation", dedupe_key=f"simulation:{tag}", name=f"Simulated Clinic {tag}",
            channel="whatsapp", contact=contact, market="UAE", niche="clinic", actor="simulation")
        steps = []
        for state in ("TRIAGED", "BUILDING"):
            steps.append(self.store.transition(lead["id"], state, actor="simulation")["description"])
        out = self.store.stage_outreach(
            lead["id"], "whatsapp", f"Hi! I built a 24/7 WhatsApp assistant demo for {lead['name']}.",
            actor="simulation")
        steps.append(self.store.transition(lead["id"], "STAGED", actor="simulation",
                                           payload={"outreach_id": out["id"]})["description"])
        return {"success": True, "result": f"Simulated lead {lead['id']}: " + ", ".join(steps),
                "lead_id": lead["id"], "outreach_id": out["id"], "steps": steps}
