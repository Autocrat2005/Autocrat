"""
Autocrat — Core Engine
The brain of the system. Routes commands, manages plugins, tracks history.
Now with AI Brain for intent classification and behavioral learning.
"""

import importlib
import time
import threading
import re
import secrets
import concurrent.futures
from typing import Any, Dict, List, Optional
from datetime import datetime
from nexus.core.logger import get_logger
from nexus.core.parser import CommandParser, ParsedCommand
from nexus.core.plugin import NexusPlugin
from nexus.core.events import EventBus
from nexus.core.config import Config
from nexus.core.brain import NexusBrain
from nexus.core.learner import BehaviorLearner
from nexus.core.ai_engine import GeminiEngine
from nexus.core.personality import Personality

log = get_logger("engine")


class NexusEngine:
    """Central command router and plugin manager."""

    def __init__(self):
        self.config = Config()
        self.events = EventBus()
        self.parser = CommandParser()
        self.plugins: Dict[str, NexusPlugin] = {}
        self.history: List[Dict[str, Any]] = []
        self._max_history = 1000
        self._last_command = None
        self._last_action = None
        self._last_result = None
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}

        # Session context for pronoun resolution ("close it", "open that folder")
        self._context: Dict[str, Optional[str]] = {
            "app": None,       # last mentioned app/process
            "file": None,      # last mentioned file/path
            "folder": None,    # last mentioned folder
            "url": None,       # last mentioned URL/website
            "query": None,     # last search query
            "window": None,    # last mentioned window title
        }

        # JARVIS-style personality
        self.personality = Personality()

        # AI Brain + Behavioral Learner
        self.brain = NexusBrain()
        self.learner = BehaviorLearner()

        # Gemini LLM Engine (smart fallback + conversational)
        gemini_key = self.config.get("ai", "gemini_api_key") or ""
        llm_backend = self.config.get("ai", "llm_backend") or "local_ollama"
        local_model = self.config.get("ai", "local_model") or "qwen2.5-coder:3b"
        local_base_url = self.config.get("ai", "local_base_url") or "http://127.0.0.1:11434"
        gemini_enabled = bool(self.config.get("ai", "gemini_enabled"))
        strict_json_mode = bool(self.config.get("ai", "strict_json_mode", True))
        local_retry_count = int(self.config.get("ai", "local_retry_count", 2) or 0)
        use_native_tools = bool(self.config.get("ai", "use_native_tools", True))
        self.gemini = GeminiEngine(
            api_key=gemini_key,
            llm_backend=llm_backend,
            local_model=local_model,
            local_base_url=local_base_url,
            gemini_enabled=gemini_enabled,
            strict_json_mode=strict_json_mode,
            local_retry_count=local_retry_count,
            use_native_tools=use_native_tools,
        )

        # Load brain model in background thread (takes a few seconds)
        threading.Thread(target=self.brain.initialize, daemon=True).start()

        log.info("NEXUS Engine initialized")

    def load_plugin(self, plugin: NexusPlugin):
        """Register a plugin instance."""
        self.plugins[plugin.name] = plugin
        log.info(f"Plugin loaded: {plugin.icon} {plugin.name} ({len(plugin.get_commands())} commands)")
        self.events.emit("plugin.loaded", {"name": plugin.name})

    def unload_plugin(self, name: str):
        """Unload a plugin by name."""
        if name in self.plugins:
            del self.plugins[name]
            log.info(f"Plugin unloaded: {name}")
            self.events.emit("plugin.unloaded", {"name": name})

    def load_all_plugins(self):
        """Auto-load all built-in plugins."""
        plugin_modules = [
            ("nexus.plugins.window_manager", "WindowManagerPlugin"),
            ("nexus.plugins.process_controller", "ProcessControllerPlugin"),
            ("nexus.plugins.file_ops", "FileOpsPlugin"),
            ("nexus.plugins.keyboard_mouse", "KeyboardMousePlugin"),
            ("nexus.plugins.screen_intel", "ScreenIntelPlugin"),
            ("nexus.plugins.app_launcher", "AppLauncherPlugin"),
            ("nexus.plugins.clipboard", "ClipboardPlugin"),
            ("nexus.plugins.system_info", "SystemInfoPlugin"),
            ("nexus.plugins.volume_display", "VolumeDisplayPlugin"),
            ("nexus.plugins.shell_executor", "ShellExecutorPlugin"),
            ("nexus.plugins.task_scheduler", "TaskSchedulerPlugin"),
            ("nexus.plugins.workflow_engine", "WorkflowEnginePlugin"),
            ("nexus.plugins.smart_actions", "SmartActionsPlugin"),
            ("nexus.plugins.power_tools", "PowerToolsPlugin"),
            ("nexus.plugins.intelligence", "IntelligencePlugin"),
            ("nexus.plugins.comet_web_agent", "CometWebAgentPlugin"),
            ("nexus.plugins.core_builder", "CoreBuilderPlugin"),
        ]

        enabled = self.config.get("plugins", "enabled") or []

        for module_path, class_name in plugin_modules:
            plugin_name = module_path.split(".")[-1]
            if enabled and plugin_name not in enabled:
                log.debug(f"Skipping disabled plugin: {plugin_name}")
                continue
            try:
                module = importlib.import_module(module_path)
                plugin_class = getattr(module, class_name)
                plugin = plugin_class()
                self.load_plugin(plugin)
            except Exception as e:
                log.warning(f"Failed to load plugin {plugin_name}: {e}")

        log.info(f"Loaded {len(self.plugins)} plugins with "
                 f"{sum(len(p.get_commands()) for p in self.plugins.values())} total commands")

        # Inject engine reference into all plugins (needed by core_builder for hot-loading)
        for plugin in self.plugins.values():
            plugin._engine = self

        # Auto-load previously generated plugins (via core_builder)
        builder = self.plugins.get("core_builder")
        if builder and hasattr(builder, "auto_load_all_generated"):
            try:
                builder.auto_load_all_generated()
            except Exception as e:
                log.warning(f"Auto-load generated plugins failed: {e}")

        # Feed available commands to Gemini for dynamic understanding
        self.gemini.update_commands(self.get_all_commands())

    def execute(self, text: str) -> Dict[str, Any]:
        """
        5-stage intelligent command pipeline:
          0. Preprocessing — pronoun resolution, context injection
          1. Regex parser (fast, exact) — with typo correction & abbreviations
          2. ML Brain — sentence-transformer embeddings
          3. LLM — native tool calling / strict JSON
          4. Conversational fallback
        """
        start = time.time()
        timestamp = datetime.now().isoformat()

        # Guard: reject empty or absurdly long input
        text = text.strip()
        if not text:
            return {"success": False, "error": "Empty command", "timestamp": timestamp, "duration_ms": 0}
        if len(text) > 2000:
            return {"success": False, "error": "Command too long (max 2000 chars)", "timestamp": timestamp, "duration_ms": 0}

        # Session memory — handle follow-up commands
        text_lower = text.lower().strip()
        if text_lower in ("do that again", "repeat", "again", "redo") and self._last_command:
            text = self._last_command
        elif text_lower in ("what did i just do", "what was that", "last command"):
            if self._last_command:
                return {"success": True, "result": f"Your last command was: '{self._last_command}'",
                        "narration": f"That was '{self._last_command}', sir.",
                        "timestamp": timestamp, "duration_ms": 0}
            else:
                return {"success": True, "result": "No previous commands in this session.",
                        "narration": "Nothing yet — this session just started.",
                        "timestamp": timestamp, "duration_ms": 0}

        # ── Stage 0: Pronoun / context resolution ──
        text = self._resolve_context(text)

        # Handle approval / rejection commands
        approval_result = self._handle_confirmation_command(text, timestamp)
        if approval_result is not None:
            return approval_result

        # ── Stage 1: Regex parser (fast, exact) ──
        parsed_commands = self.parser.parse(text)

        # ── Stage 2: ML Brain (semantic similarity) ──
        intent_match = None
        if not parsed_commands:
            boost = self.learner.get_boost_map()
            success_rates = self.learner.get_success_rates()
            intent_match = self.brain.classify(text, boost_map=boost, success_rates=success_rates)

            if intent_match:
                confidence = intent_match['confidence']
                log.info(f"🧠 ML Brain matched: {intent_match['intent']} "
                         f"(confidence: {confidence})")

                # If confidence is marginal AND Gemini is available AND query is complex,
                # defer to LLM for better understanding
                is_complex = len(text.split()) >= 7 or any(w in text.lower() for w in [
                    'first', 'best', 'latest', 'specific', 'particular', 'from', 'by',
                    'between', 'and then', 'after that', 'switch to', 'tab',
                ])
                defer_to_llm = (confidence < 0.65 and is_complex and self.gemini._ready)

                if defer_to_llm:
                    log.info(f"🔀 Deferring to Gemini (ML confidence {confidence:.3f} too low for complex query)")
                    intent_match = None  # Clear so Stage 3 runs
                else:
                    action_parts = intent_match["action"].split(".")
                    if len(action_parts) == 2:
                        parsed_commands = [ParsedCommand(
                            plugin=action_parts[0],
                            action=action_parts[1],
                            args=intent_match.get("params", {}),
                        )]

        # ── Stage 3: Gemini LLM (dynamic understanding) ──
        llm_result = None
        if not parsed_commands:
            llm_result = self.gemini.process(text)
            if llm_result:
                action = llm_result.get("action", "")

                # 3a: Conversational response (general knowledge / chat)
                if action == "__conversation__":
                    elapsed = round((time.time() - start) * 1000, 2)
                    result = {
                        "success": True,
                        "result": llm_result.get("response", ""),
                        "ai_source": llm_result.get("source", "gemini"),
                        "timestamp": timestamp,
                        "duration_ms": elapsed,
                    }
                    self._record_history(text, result)
                    self._last_command = text
                    return result

                # 3b: LLM mapped to an action
                if action and "." in action:
                    log.info(f"🤖 LLM matched: {action} "
                             f"(confidence: {llm_result.get('confidence', '?')}, "
                             f"source: {llm_result.get('source', '?')})")
                    action_parts = action.split(".")
                    parsed_commands = [ParsedCommand(
                        plugin=action_parts[0],
                        action=action_parts[1],
                        args=llm_result.get("params", {}),
                    )]
                    intent_match = {
                        "intent": f"llm_{action}",
                        "action": action,
                        "confidence": llm_result.get("confidence", 0.8),
                    }

                    # Handle multi-step plans from LLM
                    if llm_result.get("multi_step") and len(llm_result["multi_step"]) > 1:
                        parsed_commands = []
                        for step in llm_result["multi_step"]:
                            parts = step["action"].split(".")
                            if len(parts) == 2:
                                parsed_commands.append(ParsedCommand(
                                    plugin=parts[0], action=parts[1],
                                    args=step.get("params", {}),
                                ))

        # ── Nothing matched at all ──
        if not parsed_commands:
            suggestions = []
            if self._last_command:
                chain_suggestions = self.learner.get_chain_suggestions(self._last_command)
                suggestions.extend([s["command"] for s in chain_suggestions])

            result = {
                "success": False,
                "error": f"Could not understand: '{text}'",
                "hint": "Try natural language like 'search youtube for lofi' or ask me anything!",
                "suggestions": suggestions if suggestions else None,
                "timestamp": timestamp,
                "duration_ms": 0,
            }
            self._record_history(text, result)
            return result

        # Hard-block certain actions regardless of confirmation flow
        blocked = self._get_blocked_actions(parsed_commands)
        if blocked:
            return {
                "success": False,
                "blocked": True,
                "error": "Action blocked by safety policy",
                "blocked_actions": blocked,
                "timestamp": timestamp,
                "duration_ms": round((time.time() - start) * 1000, 2),
            }

        # Intercept destructive actions for confirmation before execution
        needs_confirm, reasons = self._requires_confirmation(parsed_commands)
        if needs_confirm:
            confirmation_id = self._create_pending_confirmation(text, parsed_commands, reasons)
            return {
                "success": False,
                "requires_confirmation": True,
                "confirmation_id": confirmation_id,
                "message": "Destructive/sensitive action requires confirmation",
                "reasons": reasons,
                "approve_command": f"approve {confirmation_id}",
                "reject_command": f"reject {confirmation_id}",
                "timestamp": timestamp,
                "duration_ms": round((time.time() - start) * 1000, 2),
            }

        # Execute parsed commands (supports parallel execution for independent steps)
        results = []
        overall_success = True

        # Determine if steps can run in parallel (different plugins = independent)
        can_parallelize = (
            len(parsed_commands) > 1
            and len({p.plugin for p in parsed_commands}) == len(parsed_commands)
            and all(p.plugin != "_meta" for p in parsed_commands)
        )

        if can_parallelize:
            log.info(f"⚡ Running {len(parsed_commands)} independent steps in parallel")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(parsed_commands), 4)) as pool:
                future_map = {
                    pool.submit(self._execute_plugin_command, parsed): i
                    for i, parsed in enumerate(parsed_commands)
                }
                indexed_results = [None] * len(parsed_commands)
                for future in concurrent.futures.as_completed(future_map):
                    idx = future_map[future]
                    try:
                        r = future.result()
                    except Exception as e:
                        r = {"success": False, "error": str(e)}
                    indexed_results[idx] = r
                results = indexed_results
        else:
            for parsed in parsed_commands:
                if parsed.plugin == "_meta":
                    r = self._handle_meta(parsed)
                else:
                    r = self._execute_plugin_command(parsed)
                results.append(r)

        for r in results:
            if not r.get("success", False):
                overall_success = False

        elapsed = round((time.time() - start) * 1000, 2)

        if len(results) == 1:
            final = results[0]
        else:
            final = {
                "success": overall_success,
                "results": results,
                "chain_length": len(results),
            }

        final["timestamp"] = timestamp
        final["duration_ms"] = elapsed

        # Add AI info if brain was used
        if intent_match:
            final["ai_match"] = {
                "intent": intent_match["intent"],
                "confidence": intent_match["confidence"],
            }

        # ── JARVIS narration layer ──
        if final.get("success"):
            action_name = intent_match["action"] if intent_match else ""
            target = ""
            # Extract target from parsed args for narration
            if parsed_commands:
                args = parsed_commands[0].args or {}
                for k in ("app_name", "query", "target", "title", "path"):
                    if k in args and args[k]:
                        target = str(args[k])
                        break

            if len(results) > 1:
                final["narration"] = self.personality.narrate_multi(results, elapsed)
            else:
                final["narration"] = self.personality.narrate(
                    action=action_name, result=final,
                    target=target, duration_ms=elapsed,
                )
        else:
            final["narration"] = self.personality.narrate(action="", result=final)

        # Record for learning
        self._record_history(text, final)
        self.learner.record(
            command=text,
            intent=intent_match["intent"] if intent_match else None,
            success=final.get("success", False),
            duration_ms=elapsed,
            active_window=self._get_active_window_title(),
        )
        self._last_command = text
        self._last_action = intent_match["action"] if intent_match else None
        self._last_result = final

        # Update session context from this execution
        self._update_context(text, parsed_commands, final)

        # Inject proactive suggestions for successful actions
        if final.get("success") and not final.get("suggestions"):
            chain_s = self.learner.get_chain_suggestions(text)
            if chain_s:
                final["suggestions"] = [s["command"] for s in chain_s[:3]]

        self.events.emit("command.executed", {"text": text, "result": final})
        return final

    # ─── Pronoun / Context Resolution ─────────────────────────────────────────

    _PRONOUNS = re.compile(
        r"\b(it|that|this|those|them|"
        r"that app|that file|that folder|that window|that site|"
        r"the same one|the same app|the same file)\b",
        re.IGNORECASE,
    )

    _ACTION_CONTEXT_MAP = {
        # verb → which context slot to substitute
        "close": "app", "kill": "app", "stop": "app", "quit": "app",
        "open": "app", "launch": "app", "start": "app", "focus": "window",
        "switch": "window", "minimize": "window", "maximize": "window",
        "delete": "file", "remove": "file", "copy": "file", "move": "file",
        "search": "query", "google": "query", "find": "query",
    }

    def _resolve_context(self, text: str) -> str:
        """Replace pronouns (it, that, this) with actual context from the session.

        Examples:
          - After "open Chrome", "close it" → "close Chrome"
          - After "search youtube for lofi", "search for that" → "search for lofi"
          - After "find file report.pdf", "delete it" → "delete report.pdf"
        """
        if not self._PRONOUNS.search(text):
            return text

        text_lower = text.lower().strip()
        words = text_lower.split()
        if not words:
            return text

        # Determine which context slot to use based on the verb
        verb = words[0]
        slot = self._ACTION_CONTEXT_MAP.get(verb)

        # If no specific mapping, try to guess from available context
        if not slot:
            # Check if any context is available at all
            for candidate in ("app", "window", "file", "query", "url", "folder"):
                if self._context.get(candidate):
                    slot = candidate
                    break

        if not slot or not self._context.get(slot):
            return text

        replacement = self._context[slot]

        # Replace the pronoun with the context value
        resolved = self._PRONOUNS.sub(replacement, text, count=1)
        if resolved != text:
            log.info(f"🔗 Context resolved: '{text}' → '{resolved}'")
        return resolved

    def _update_context(self, text: str, parsed_commands: List[ParsedCommand], result: Dict):
        """Extract entities from the executed command and update session context."""
        if not parsed_commands or not result.get("success"):
            return

        for parsed in parsed_commands:
            args = parsed.args or {}
            plugin = parsed.plugin
            action = parsed.action

            # App context
            if plugin in ("app_launcher", "process_controller"):
                for key in ("app_name", "query", "target", "path"):
                    if key in args and args[key]:
                        self._context["app"] = str(args[key])
                        break

            # Window context
            if plugin == "window_manager":
                for key in ("title", "query", "target"):
                    if key in args and args[key]:
                        self._context["window"] = str(args[key])
                        break

            # File context
            if plugin == "file_ops":
                for key in ("path", "src", "pattern", "query"):
                    if key in args and args[key]:
                        self._context["file"] = str(args[key])
                        break
                if "directory" in args:
                    self._context["folder"] = str(args["directory"])

            # URL / website context
            if plugin == "smart_actions" and action in ("open_website", "web_search_google", "web_search_youtube"):
                if "query" in args:
                    val = str(args["query"])
                    if "." in val or "://" in val:
                        self._context["url"] = val
                    else:
                        self._context["query"] = val

            # Generic query context (search queries etc.)
            if "query" in args and args["query"] and plugin not in ("_meta",):
                self._context["query"] = str(args["query"])

    def _handle_confirmation_command(self, text: str, timestamp: str) -> Optional[Dict[str, Any]]:
        """Handle approve/reject commands for pending confirmations."""
        approve_match = re.match(r"^\s*(?:approve|confirm|yes)\s+([A-Za-z0-9_-]{6,})\s*$", text, re.IGNORECASE)
        reject_match = re.match(r"^\s*(?:reject|deny|cancel|no)\s+([A-Za-z0-9_-]{6,})\s*$", text, re.IGNORECASE)

        if reject_match:
            token = reject_match.group(1)
            pending = self._pending_confirmations.pop(token, None)
            if not pending:
                return {
                    "success": False,
                    "error": f"No pending confirmation found for '{token}'",
                    "timestamp": timestamp,
                    "duration_ms": 0,
                }
            return {
                "success": False,
                "result": f"Rejected pending command: {pending.get('text', '')}",
                "confirmation_id": token,
                "timestamp": timestamp,
                "duration_ms": 0,
            }

        if approve_match:
            token = approve_match.group(1)
            pending = self._pending_confirmations.pop(token, None)
            if not pending:
                return {
                    "success": False,
                    "error": f"No pending confirmation found for '{token}'",
                    "timestamp": timestamp,
                    "duration_ms": 0,
                }

            parsed_commands = pending.get("parsed_commands", [])
            blocked = self._get_blocked_actions(parsed_commands)
            if blocked:
                return {
                    "success": False,
                    "blocked": True,
                    "error": "Action blocked by safety policy",
                    "blocked_actions": blocked,
                    "confirmation_id": token,
                    "timestamp": timestamp,
                    "duration_ms": 0,
                }

            results = []
            overall_success = True
            for parsed in parsed_commands:
                if parsed.plugin == "_meta":
                    r = self._handle_meta(parsed)
                else:
                    r = self._execute_plugin_command(parsed)
                results.append(r)
                if not r.get("success", False):
                    overall_success = False

            final = results[0] if len(results) == 1 else {
                "success": overall_success,
                "results": results,
                "chain_length": len(results),
            }
            final["confirmed"] = True
            final["confirmation_id"] = token
            final["timestamp"] = timestamp
            final["duration_ms"] = 0
            return final

        return None

    def _create_pending_confirmation(self, text: str, parsed_commands: List[ParsedCommand], reasons: List[str]) -> str:
        """Store a pending destructive action and return token."""
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
        self._pending_confirmations[token] = {
            "text": text,
            "parsed_commands": parsed_commands,
            "reasons": reasons,
            "created_at": time.time(),
        }
        return token

    def get_pending_confirmations(self) -> List[Dict[str, Any]]:
        """Public summary of pending confirmations for UI clients."""
        items = []
        for cid, entry in self._pending_confirmations.items():
            items.append({
                "id": cid,
                "text": entry.get("text", ""),
                "reasons": entry.get("reasons", []),
                "created_at": entry.get("created_at"),
                "approve_command": f"approve {cid}",
                "reject_command": f"reject {cid}",
            })
        items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return items

    def resolve_confirmation_phrase(self, phrase: str, confirmation_id: str = "") -> Optional[str]:
        """Map natural yes/no phrases to approve/reject commands."""
        text = (phrase or "").strip().lower()
        if not text:
            return None

        yes_words = {
            "yes", "y", "approve", "confirm", "do it", "go ahead",
            "ok", "okay", "proceed", "run it", "allow", "sure",
        }
        no_words = {
            "no", "n", "reject", "deny", "cancel", "stop", "abort",
            "dont", "don't", "never mind", "hold",
        }

        selected_id = (confirmation_id or "").strip()
        if not selected_id:
            pending = self.get_pending_confirmations()
            if not pending:
                return None
            selected_id = pending[0]["id"]

        if any(word in text for word in yes_words):
            return f"approve {selected_id}"
        if any(word in text for word in no_words):
            return f"reject {selected_id}"
        return None

    def _requires_confirmation(self, parsed_commands: List[ParsedCommand]) -> tuple[bool, List[str]]:
        """Detect destructive/sensitive actions requiring approval."""
        if not bool(self.config.get("safety", "confirm_destructive", default=True)):
            return False, []

        reasons = []

        destructive_actions = {
            "power_tools.shutdown",
            "power_tools.restart",
            "power_tools.hibernate",
            "power_tools.sleep",
            "power_tools.logoff",
            "process_controller.kill",
            "file_ops.delete",
            "shell_executor.run",
        }
        sensitive_keywords = {"delete", "drop", "kill", "shutdown", "format", "wipe", "remove"}

        for parsed in parsed_commands:
            action_name = f"{parsed.plugin}.{parsed.action}"
            arg_text = " ".join(str(v) for v in (parsed.args or {}).values()).lower()

            if action_name in destructive_actions:
                reasons.append(f"destructive action: {action_name}")
                continue

            if any(k in arg_text for k in sensitive_keywords):
                reasons.append(f"sensitive payload keyword in {action_name}")

        return (len(reasons) > 0), reasons

    def _get_blocked_actions(self, parsed_commands: List[ParsedCommand]) -> List[str]:
        """Return actions blocked by safety policy."""
        configured = self.config.get("safety", "blocked_actions") or []
        blocked_set = {str(item).strip().lower() for item in configured if str(item).strip()}
        blocked = []
        for parsed in parsed_commands:
            action_name = f"{parsed.plugin}.{parsed.action}".lower()
            if action_name in blocked_set:
                blocked.append(action_name)
        return blocked

    def _execute_plugin_command(self, parsed: ParsedCommand) -> Dict[str, Any]:
        """Execute a command on the correct plugin. Auto-heals generated plugins on failure."""
        plugin = self.plugins.get(parsed.plugin)
        if not plugin:
            return {
                "success": False,
                "error": f"Plugin '{parsed.plugin}' not loaded",
                "available_plugins": list(self.plugins.keys()),
            }

        if not plugin.enabled:
            return {
                "success": False,
                "error": f"Plugin '{parsed.plugin}' is disabled",
            }

        result = plugin.execute(parsed.action, parsed.args)

        # Auto-heal: if a generated plugin fails, ask core_builder to fix it
        if not result.get("success"):
            builder = self.plugins.get("core_builder")
            if builder and hasattr(builder, "_generated") and parsed.plugin in builder._generated:
                log.info(f"🩺 Auto-healing generated plugin '{parsed.plugin}' after error...")
                heal_result = builder.execute_with_heal(
                    parsed.plugin, parsed.action, parsed.args or {}
                )
                if heal_result.get("success"):
                    return heal_result

        return result

    def _handle_meta(self, parsed: ParsedCommand) -> Dict[str, Any]:
        """Handle built-in meta commands (help, plugins, history, status)."""
        action = parsed.action

        if action in {"toggle_mock_navigation", "enable_mock_navigation", "disable_mock_navigation", "mock_navigation_status"}:
            current = bool(self.config.get("safety", "web", "react_mock_navigation_in_safe_mode", default=False))

            if action == "mock_navigation_status":
                return {
                    "success": True,
                    "result": f"[SYSTEM] mock_navigation is {'TRUE' if current else 'FALSE'}",
                    "mock_navigation": current,
                    "control_plane": True,
                }

            if action == "toggle_mock_navigation":
                new_value = not current
            elif action == "enable_mock_navigation":
                new_value = True
            else:
                new_value = False

            self.config.set("safety", "web", "react_mock_navigation_in_safe_mode", new_value)
            self.config.save()

            return {
                "success": True,
                "result": f"[SYSTEM] mock_navigation set to {'TRUE' if new_value else 'FALSE'}",
                "mock_navigation": new_value,
                "control_plane": True,
            }

        if action == "help":
            lines = [
                "╔══════════════════════════════════════════╗",
                "║          Autocrat — Command Help         ║",
                "╚══════════════════════════════════════════╝",
                "",
            ]
            for p in self.plugins.values():
                lines.append(p.get_help())
                lines.append("")
            return {"success": True, "result": "\n".join(lines)}

        elif action == "help_plugin":
            name = parsed.args.get("plugin", "")
            plugin = self.plugins.get(name)
            if plugin:
                return {"success": True, "result": plugin.get_help()}
            # Fuzzy search
            matches = [p for p in self.plugins if name.lower() in p.lower()]
            if matches:
                return {"success": True, "result": self.plugins[matches[0]].get_help()}
            return {"success": False, "error": f"Plugin '{name}' not found"}

        elif action == "plugins":
            plugin_list = []
            for p in self.plugins.values():
                plugin_list.append({
                    "name": p.name,
                    "icon": p.icon,
                    "description": p.description,
                    "version": p.version,
                    "enabled": p.enabled,
                    "commands": len(p.get_commands()),
                })
            return {"success": True, "result": plugin_list}

        elif action == "history":
            return {"success": True, "result": self.history[-50:]}

        elif action == "status":
            brain_stats = self.learner.get_stats()
            ai_stats = self.gemini.get_stats()
            return {
                "success": True,
                "result": {
                    "plugins_loaded": len(self.plugins),
                    "total_commands": sum(len(p.get_commands()) for p in self.plugins.values()),
                    "history_size": len(self.history),
                    "ai_brain": "active (ML)" if self.brain._ready else "fallback (keyword)",
                    "llm_backend": ai_stats.get("llm_backend", "none"),
                    "llm_status": "active" if ai_stats.get("llm_active") else "heuristic-only",
                    **brain_stats,
                    **ai_stats,
                },
            }

        elif action == "suggestions":
            suggestions = []
            # Time-based
            time_s = self.learner.get_time_suggestions()
            suggestions.extend([{"type": "time", **s} for s in time_s])
            # Chain-based
            if self._last_command:
                chain_s = self.learner.get_chain_suggestions(self._last_command)
                suggestions.extend([{"type": "chain", **s} for s in chain_s])
            # Frequent
            freq = self.learner.get_frequent_commands(5)
            suggestions.extend([{"type": "frequent", **f} for f in freq])
            return {"success": True, "result": suggestions}

        return {"success": False, "error": f"Unknown meta command: {action}"}

    def _record_history(self, text: str, result: Dict[str, Any]):
        """Save command to history."""
        self.history.append({
            "command": text,
            "success": result.get("success", False),
            "timestamp": result.get("timestamp"),
            "duration_ms": result.get("duration_ms"),
        })
        if len(self.history) > self._max_history:
            self.history = self.history[-self._max_history:]

    def get_all_commands(self) -> List[Dict]:
        """Get all commands from all plugins."""
        all_cmds = []
        for plugin in self.plugins.values():
            for cmd in plugin.get_commands():
                cmd["plugin"] = plugin.name
                cmd["icon"] = plugin.icon
                all_cmds.append(cmd)
        return all_cmds

    @staticmethod
    def _get_active_window_title() -> Optional[str]:
        """Best-effort active window title for learner context."""
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            return win32gui.GetWindowText(hwnd) or None
        except Exception:
            return None
