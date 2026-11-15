"""Plugin discovery, AUTOCRAT_PLUGIN_PATHS, profiles, side_effect gate, dry run, kill switch."""

import textwrap

import pytest

from nexus.core import plugin_loader

EXTERNAL_PLUGIN = textwrap.dedent('''
    from nexus.core.plugin import NexusPlugin

    class OutboxPlugin(NexusPlugin):
        name = "outbox"
        profiles = ("core",)
        sent = []

        def setup(self):
            self.register_command("send_dm", self.send_dm, "Send a DM", side_effect="external",
                                  patterns=[r"^send dm (?P<text>.+)$"],
                                  phrases=["fire off a direct message"])
            self.register_command("charge", self.charge, "Create payment link", side_effect="financial",
                                  patterns=[r"^charge (?P<amount>\\d+)$"])
            self.register_command("peek", self.peek, "Read-only", side_effect="none",
                                  patterns=[r"^peek outbox$"], aliases=["look"])
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def send_dm(self, text="", **kwargs):
            OutboxPlugin.sent.append(text)
            return {"success": True, "result": f"sent {text}"}

        def charge(self, amount=0, **kwargs):
            return {"success": True, "result": f"charged {amount}"}

        def peek(self, **kwargs):
            return {"success": True, "result": list(OutboxPlugin.sent)}
''')

DESK_ONLY_PLUGIN = textwrap.dedent('''
    import this_module_does_not_exist  # would crash if imported on the core
    from nexus.core.plugin import NexusPlugin

    class DeskThing(NexusPlugin):
        name = "desk_thing"
        def setup(self):
            pass
''')


@pytest.fixture
def ext_dir(tmp_path, monkeypatch):
    d = tmp_path / "revenue_plugins"
    d.mkdir()
    (d / "outbox.py").write_text(EXTERNAL_PLUGIN, encoding="utf-8")
    (d / "desk_thing.py").write_text(DESK_ONLY_PLUGIN, encoding="utf-8")
    monkeypatch.setenv("AUTOCRAT_PLUGIN_PATHS", str(d))
    return d


def test_discovery_respects_profiles_without_importing(ext_dir):
    names = {s.plugin_name for s in plugin_loader.discover(profile="core")}
    assert {"pipeline", "outbox"} <= names
    assert "desk_thing" not in names          # desk-only, never imported on core
    assert "keyboard_mouse" not in names      # built-in desktop plugin skipped on core
    desk = {s.plugin_name for s in plugin_loader.discover(profile="desk")}
    assert "outbox" not in desk and "keyboard_mouse" in desk


def test_engine_loads_external_plugins_and_routes_patterns(ext_dir, make_engine):
    engine = make_engine()
    assert "outbox" in engine.plugins and "pipeline" in engine.plugins
    assert getattr(engine.plugins["outbox"], "_origin") == "external"
    result = engine.execute("peek outbox")
    assert result["success"], result
    assert any(i["intent"] == "outbox__send_dm" for i in engine.brain.catalog)


def test_external_actions_need_approval_then_simulate_in_dry_run(ext_dir, make_engine):
    engine = make_engine()
    pending = engine.execute("send dm hello there")
    assert pending.get("requires_confirmation"), pending
    assert any("external" in r for r in pending["reasons"])
    done = engine.execute(f"approve {pending['confirmation_id']}", actor="telegram:1:Yash")
    assert done["success"] and done.get("dry_run") is True
    assert engine.plugins["outbox"].sent == []                 # nothing actually sent
    audit = engine.store.audit_entries()
    assert audit[0]["action"] == "approve" and audit[0]["actor"] == "telegram:1:Yash"


def test_outbound_approval_cannot_be_disabled_by_config(ext_dir, make_engine):
    engine = make_engine()
    engine.config.set("safety", "confirm_destructive", False)
    result = engine.execute("charge 625")
    assert result.get("requires_confirmation"), "financial actions must always need approval"


def test_live_mode_executes_after_approval(ext_dir, make_engine):
    engine = make_engine()
    engine.runtime.set_run_mode("live", actor="test")
    pending = engine.execute("send dm real one")
    done = engine.execute(f"approve {pending['confirmation_id']}")
    assert done["success"] and not done.get("dry_run")
    assert engine.plugins["outbox"].sent == ["real one"]


def test_kill_switch_blocks_and_stops_workers(ext_dir, make_engine):
    engine = make_engine()
    engine.runtime.set_run_mode("live", actor="test")
    engine.start_workers()
    assert engine.plugins["outbox"].started
    result = engine.execute("kill switch")
    assert result["success"] and result["kill_switch"]
    assert not engine.plugins["outbox"].started
    pending = engine.execute("send dm while killed")
    done = engine.execute(f"approve {pending['confirmation_id']}")
    assert not done["success"] and done.get("kill_switch")
    assert engine.execute("peek outbox")["success"]              # read-only still works
    # Releasing requires approval, and the state persists in the store
    release = engine.execute("resume")
    assert release.get("requires_confirmation")
    assert engine.store.kv_get("kill_switch") == "engaged"
    engine.execute(f"approve {release['confirmation_id']}")
    assert not engine.runtime.killed and engine.store.kv_get("kill_switch") == "released"


def test_switching_to_live_requires_approval(make_engine):
    engine = make_engine()
    result = engine.execute("run mode live")
    assert result.get("requires_confirmation")
    assert engine.runtime.run_mode == "dry_run"
    engine.execute(f"approve {result['confirmation_id']}")
    assert engine.runtime.run_mode == "live"
    assert engine.execute("run mode dry_run")["run_mode"] == "dry_run"   # going safer needs no approval


def test_alias_cannot_bypass_side_effect_gate(ext_dir, make_engine):
    engine = make_engine()
    from nexus.core.parser import ParsedCommand
    cmd = ParsedCommand(plugin="outbox", action="look", args={})
    assert engine._canonical_action(cmd) == "outbox.peek"


def test_invalid_side_effect_rejected():
    from nexus.core.plugin import NexusPlugin

    class Bad(NexusPlugin):
        name = "bad"

        def setup(self):
            self.register_command("x", lambda: None, side_effect="sometimes")

    with pytest.raises(ValueError):
        Bad()


def test_autocorrect_never_rewrites_a_command_that_already_parses():
    from nexus.core.parser import CommandParser
    parser = CommandParser()
    parsed = parser.parse("run mode live")
    assert [(p.plugin, p.action) for p in parsed] == [("_meta", "run_mode")]
    assert parser.parse("kill switch")[0].action == "kill_switch"      # not process_controller.kill
