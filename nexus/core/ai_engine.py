"""
Autocrat — AI Engine (LLM Integration)
Uses a configurable LLM backend for fallback understanding:
    - local_ollama (default, on-device)
    - gemini (optional cloud backend)

The LLM maps unrecognized natural language into available plugin actions,
composes multi-step plans, and can answer general questions conversationally.

Also provides a learning memory: every successful LLM mapping is cached
so the same (or similar) command is instant next time — no LLM call needed.
"""

import json
import os
import re
import sqlite3
import time
import hashlib
import threading
from typing import Any, Dict, List, Optional, Tuple
import httpx
from nexus.core.logger import get_logger

log = get_logger("ai_engine")

# ──────────────────────────────────────────────────────────────────────
# Learning Memory — cached LLM mappings so repeat queries are instant
# ──────────────────────────────────────────────────────────────────────

class LearningMemory:
    """SQLite cache of LLM-resolved commands. Grows smarter over time."""

    def __init__(self, db_path: str = "nexus_brain.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS learned_commands (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                input_text  TEXT NOT NULL,
                input_hash  TEXT NOT NULL,
                action      TEXT NOT NULL,
                params      TEXT DEFAULT '{}',
                confidence  REAL DEFAULT 0.85,
                used_count  INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                last_used   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS llm_conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_msg    TEXT NOT NULL,
                llm_reply   TEXT NOT NULL,
                was_action  INTEGER DEFAULT 0,
                action      TEXT,
                timestamp   TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_learned_hash
                ON learned_commands(input_hash);
            CREATE INDEX IF NOT EXISTS idx_learned_text
                ON learned_commands(input_text);
        """)
        self.conn.commit()

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text for fuzzy matching."""
        t = text.lower().strip()
        t = re.sub(r"[^\w\s]", "", t)
        t = re.sub(r"\s+", " ", t)
        return t

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def lookup(self, text: str) -> Optional[Dict]:
        """Check if we've already learned how to handle this command."""
        norm = self._normalize(text)
        h = self._hash(norm)

        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM learned_commands WHERE input_hash = ? ORDER BY used_count DESC LIMIT 1",
                (h,)
            ).fetchone()

            if row:
                self.conn.execute(
                    "UPDATE learned_commands SET used_count = used_count + 1, last_used = datetime('now') WHERE id = ?",
                    (row["id"],)
                )
                self.conn.commit()
                return {
                    "action": row["action"],
                    "params": json.loads(row["params"]),
                    "confidence": min(row["confidence"] + row["used_count"] * 0.01, 0.99),
                    "source": "learned_memory",
                    "times_used": row["used_count"],
                }

            # Fuzzy: check for very similar commands (word overlap > 80%)
            norm_words = set(norm.split())
            if len(norm_words) >= 2:
                rows = self.conn.execute(
                    "SELECT *, input_text FROM learned_commands WHERE used_count >= 2 ORDER BY used_count DESC LIMIT 100"
                ).fetchall()
                for r in rows:
                    r_words = set(self._normalize(r["input_text"]).split())
                    if r_words and norm_words:
                        overlap = len(norm_words & r_words) / max(len(norm_words | r_words), 1)
                        if overlap > 0.80:
                            self.conn.execute(
                                "UPDATE learned_commands SET used_count = used_count + 1, last_used = datetime('now') WHERE id = ?",
                                (r["id"],)
                            )
                            self.conn.commit()
                            return {
                                "action": r["action"],
                                "params": json.loads(r["params"]),
                                "confidence": min(r["confidence"] + 0.02, 0.95),
                                "source": "learned_memory_fuzzy",
                            }
        return None

    def remember(self, text: str, action: str, params: dict, confidence: float = 0.85):
        """Save a successful LLM-resolved mapping for future use."""
        norm = self._normalize(text)
        h = self._hash(norm)
        with self._lock:
            existing = self.conn.execute("SELECT id FROM learned_commands WHERE input_hash = ?", (h,)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE learned_commands SET used_count = used_count + 1, last_used = datetime('now') WHERE id = ?",
                    (existing["id"],)
                )
            else:
                self.conn.execute(
                    "INSERT INTO learned_commands (input_text, input_hash, action, params, confidence) VALUES (?,?,?,?,?)",
                    (text, h, action, json.dumps(params), confidence)
                )
            self.conn.commit()

    def log_conversation(self, user_msg: str, llm_reply: str, was_action: bool = False, action: str = None):
        """Log an LLM conversation for context and debugging."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO llm_conversations (user_msg, llm_reply, was_action, action) VALUES (?,?,?,?)",
                (user_msg, llm_reply, int(was_action), action)
            )
            self.conn.commit()

    def get_stats(self) -> Dict:
        """Return learning statistics."""
        cur = self.conn.cursor()
        learned = cur.execute("SELECT COUNT(*) FROM learned_commands").fetchone()[0]
        total_uses = cur.execute("SELECT COALESCE(SUM(used_count),0) FROM learned_commands").fetchone()[0]
        conversations = cur.execute("SELECT COUNT(*) FROM llm_conversations").fetchone()[0]
        return {
            "learned_commands": learned,
            "total_cache_hits": total_uses,
            "llm_conversations": conversations,
        }


# ──────────────────────────────────────────────────────────────────────
# Gemini LLM Engine — the smart fallback
# ──────────────────────────────────────────────────────────────────────

class GeminiEngine:
    """
    Connects to Google Gemini (free tier) or local Ollama for dynamic command understanding.
    When the local classifiers can't figure out what the user wants, this
    engine asks the LLM to map natural language → available actions.
    
    Supports two modes for Ollama:
      - Native tool calling via /api/chat (preferred, faster, no JSON repair needed)
      - Strict JSON prompt engineering (fallback for older models)
    
    If no API key is configured, falls back to enhanced local heuristics.
    """

    def __init__(
        self,
        api_key: str = None,
        available_commands: List[Dict] = None,
        llm_backend: str = "local_ollama",
        local_model: str = "qwen2.5-coder:3b",
        local_base_url: str = "http://127.0.0.1:11434",
        gemini_enabled: bool = False,
        strict_json_mode: bool = True,
        local_retry_count: int = 2,
        use_native_tools: bool = True,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = None
        self._ready = False
        self.available_commands = available_commands or []
        self.memory = LearningMemory()
        self.llm_backend = (llm_backend or "local_ollama").strip().lower()
        self.local_model = local_model
        self.local_base_url = local_base_url.rstrip("/")
        self.gemini_enabled = bool(gemini_enabled)
        self.strict_json_mode = bool(strict_json_mode)
        self.local_retry_count = max(0, int(local_retry_count or 0))
        self.use_native_tools = bool(use_native_tools)
        self._supports_native_tools: Optional[bool] = None  # auto-detected

        # Multi-turn conversation context (sliding window of last N turns)
        self._conversation_history: List[Dict[str, str]] = []
        self._max_conversation_turns = 6  # keep last 6 user/assistant pairs
        self._init_backend()

    def _init_backend(self):
        """Initialize selected backend (local first)."""
        if self.llm_backend == "local_ollama":
            self._ready = self._check_ollama_ready()
            if self._ready:
                log.info(f"🤖 Local LLM ready (Ollama: {self.local_model})")
            else:
                log.info(
                    f"Local LLM not reachable at {self.local_base_url} — "
                    "falling back to enhanced local heuristics"
                )
            return

        if self.llm_backend == "gemini":
            if not self.gemini_enabled:
                log.info("Gemini backend selected but disabled by config")
                self._ready = False
                return
            self._init_gemini()
            return

        log.warning(f"Unknown LLM backend '{self.llm_backend}' — using heuristics only")
        self._ready = False

    def _init_gemini(self):
        """Initialize Gemini model when explicitly enabled."""
        if not self.api_key:
            log.info("No Gemini API key — Gemini backend unavailable")
            self._ready = False
            return

        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
            self._ready = True
            log.info("🤖 Gemini backend ready (gemini-2.0-flash)")
        except Exception as e:
            log.warning(f"Gemini init failed: {e}")
            self._ready = False

    def _check_ollama_ready(self) -> bool:
        """Check if local Ollama API and model are available."""
        try:
            with httpx.Client(timeout=3.0) as client:
                tags = client.get(f"{self.local_base_url}/api/tags")
                if tags.status_code != 200:
                    return False
                data = tags.json() if tags.text else {}
                models = data.get("models", [])
                names = {m.get("name", "") for m in models}
                return self.local_model in names
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    # Multi-turn Conversation Memory
    # ──────────────────────────────────────────────────────────────────

    def _push_turn(self, role: str, content: str):
        """Add a message to the sliding conversation window."""
        self._conversation_history.append({"role": role, "content": content})
        # Trim to max turns (each turn = user + assistant = 2 messages)
        max_msgs = self._max_conversation_turns * 2
        if len(self._conversation_history) > max_msgs:
            self._conversation_history = self._conversation_history[-max_msgs:]

    def _get_conversation_messages(self, system_content: str, user_text: str) -> List[Dict[str, str]]:
        """Build message list with system prompt + conversation history + current user message."""
        messages = [{"role": "system", "content": system_content}]
        # Add recent conversation history for multi-turn context
        messages.extend(self._conversation_history)
        messages.append({"role": "user", "content": user_text})
        return messages

    # ──────────────────────────────────────────────────────────────────
    # Smart Context Window — send only relevant commands to the LLM
    # ──────────────────────────────────────────────────────────────────

    def _filter_relevant_commands(self, user_text: str, max_commands: int = 30) -> List[Dict]:
        """Pre-filter commands by keyword relevance to reduce prompt token count.

        Instead of sending all 160+ commands, we score each command against the
        user's text using word overlap + synonym expansion, then return only the
        top-N most relevant ones plus a small set of high-utility defaults.
        """
        if not self.available_commands:
            return []

        user_words = set(re.sub(r"[^\w\s]", "", user_text.lower()).split())
        if not user_words:
            return self.available_commands[:max_commands]

        # Synonym expansion for common user intents
        _synonyms = {
            "open": {"launch", "start", "run", "open"},
            "close": {"close", "kill", "terminate", "stop", "end", "quit"},
            "find": {"find", "search", "locate", "look", "where"},
            "delete": {"delete", "remove", "erase", "trash", "rm"},
            "copy": {"copy", "duplicate", "clipboard"},
            "move": {"move", "rename", "mv"},
            "write": {"write", "create", "save", "make", "new"},
            "read": {"read", "show", "display", "cat", "get", "view"},
            "list": {"list", "ls", "dir", "show", "all"},
            "volume": {"volume", "sound", "audio", "mute", "unmute", "loud", "quiet"},
            "window": {"window", "tab", "focus", "minimize", "maximize", "snap", "resize"},
            "web": {"web", "browser", "navigate", "url", "site", "http", "scrape", "react"},
            "build": {"build", "generate", "create", "plugin", "make"},
            "workflow": {"workflow", "chain", "multi", "step", "automate", "sequence"},
            "system": {"system", "cpu", "ram", "disk", "memory", "battery", "info"},
            "power": {"power", "shutdown", "restart", "sleep", "hibernate", "lock"},
            "screenshot": {"screenshot", "screen", "capture", "ocr"},
            "type": {"type", "keyboard", "key", "hotkey", "press", "shortcut"},
            "schedule": {"schedule", "cron", "timer", "every", "recurring"},
        }

        expanded_words = set(user_words)
        for word in user_words:
            for _key, synonyms in _synonyms.items():
                if word in synonyms:
                    expanded_words |= synonyms

        scored = []
        for cmd in self.available_commands:
            cmd_text = " ".join([
                cmd.get("name", "").replace("_", " "),
                cmd.get("description", ""),
                cmd.get("usage", ""),
                cmd.get("plugin", ""),
            ]).lower()
            cmd_words = set(re.sub(r"[^\w\s]", "", cmd_text).split())

            if not cmd_words:
                continue

            overlap = len(expanded_words & cmd_words)
            # Boost exact plugin name matches
            plugin_name = cmd.get("plugin", "").lower().replace("_", "")
            for uw in user_words:
                if uw in plugin_name or plugin_name.startswith(uw):
                    overlap += 3

            scored.append((overlap, cmd))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [cmd for score, cmd in scored[:max_commands] if score > 0]

        # Always include a few high-utility defaults even if not keyword-matched
        default_plugins = {"smart_actions", "app_launcher", "file_ops", "shell_executor"}
        default_cmds = [c for c in self.available_commands
                        if c.get("plugin") in default_plugins
                        and c not in top][:5]

        result = top + default_cmds
        if not result:
            # Fallback: return top commands alphabetically
            return self.available_commands[:max_commands]
        return result

    # ──────────────────────────────────────────────────────────────────
    # Native Ollama Tool Calling via /api/chat
    # ──────────────────────────────────────────────────────────────────

    def _build_ollama_tools(self, commands: List[Dict] = None) -> List[Dict]:
        """Convert available commands into Ollama /api/chat tool definitions.

        Each plugin.command becomes a tool with:
          - name: "plugin__command" (double underscore separator)
          - description: from plugin command description
          - parameters: {"query": {"type": "string"}} (generic)
        """
        cmds = commands or self.available_commands
        tools = []
        for cmd in cmds:
            plugin = cmd.get("plugin", "unknown")
            name = cmd.get("name", "unknown")
            desc = cmd.get("description", "")
            usage = cmd.get("usage", "")
            tool_name = f"{plugin}__{name}"

            # Build parameter schema from usage pattern
            properties = {}
            required = []

            # Common parameter extraction from usage strings
            if usage:
                usage_lower = usage.lower()
                if "<url>" in usage_lower or "<query>" in usage_lower or "<path>" in usage_lower:
                    properties["query"] = {
                        "type": "string",
                        "description": "The main argument (URL, search query, file path, etc.)"
                    }
                    required.append("query")
                if "<target>" in usage_lower or "<title>" in usage_lower:
                    properties["target"] = {
                        "type": "string",
                        "description": "Target identifier (window title, process name, etc.)"
                    }
                if "<src>" in usage_lower:
                    properties["src"] = {"type": "string", "description": "Source path"}
                if "<dst>" in usage_lower or "<dest>" in usage_lower:
                    properties["dst"] = {"type": "string", "description": "Destination path"}

            # Default: every tool accepts at least a generic "query" param
            if not properties:
                properties["query"] = {
                    "type": "string",
                    "description": "The main argument for this command"
                }

            tools.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": f"[{plugin}] {desc}" + (f" (usage: {usage})" if usage else ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return tools

    def _ollama_chat_with_tools(self, user_text: str, commands: List[Dict] = None) -> Optional[Dict]:
        """Call Ollama /api/chat with native tool definitions.

        Returns parsed action dict on tool_call, conversational response otherwise.
        This is significantly faster than JSON prompt engineering because:
        1. The model natively understands tool schemas (no prompt parsing needed)
        2. No JSON repair loop — the output is structured by the model itself
        3. Smaller prompt size (tool definitions are compact)
        4. Multi-turn context included for follow-up understanding
        """
        tools = self._build_ollama_tools(commands)
        system_msg = (
            "You are the AI brain of Autocrat, a Windows PC automation system. "
            "The user gives natural language commands. Use the provided tools to "
            "perform actions. If no tool fits, respond conversationally. "
            "For multi-step tasks, you may call multiple tools."
        )
        messages = self._get_conversation_messages(system_msg, user_text)
        payload = {
            "model": self.local_model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0.05, "num_predict": 500},
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(f"{self.local_base_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            log.warning(f"Ollama /api/chat failed: {e}")
            return None

        message = data.get("message", {})
        tool_calls = message.get("tool_calls", [])

        if tool_calls:
            valid_actions = {f"{c['plugin']}.{c['name']}" for c in self.available_commands}
            steps = []

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})

                # Convert "plugin__command" back to "plugin.command"
                action = tool_name.replace("__", ".", 1)
                action, params = self._normalize_action(action, tool_args)

                if action in valid_actions:
                    steps.append({"action": action, "params": params})
                else:
                    log.warning(f"Native tool call returned invalid action: {action}")

            if steps:
                log.info(f"🔧 Native tool call: {[s['action'] for s in steps]}")
                # Record turn for multi-turn context
                self._push_turn("user", user_text)
                self._push_turn("assistant", f"[tool_call: {steps[0]['action']}]")
                result = {
                    "action": steps[0]["action"],
                    "params": steps[0]["params"],
                    "confidence": 0.90,
                    "source": "native_tool_call",
                }
                if len(steps) > 1:
                    result["multi_step"] = steps
                return result

        # No tool call — it's a conversational response
        content = message.get("content", "").strip()
        if content:
            # Record turn for multi-turn context
            self._push_turn("user", user_text)
            self._push_turn("assistant", content[:500])
            return {
                "action": "__conversation__",
                "response": content,
                "confidence": 0.80,
                "source": "native_tool_call",
            }

        return None

    def _detect_native_tool_support(self) -> bool:
        """Probe whether the loaded Ollama model supports native tool calling.

        We send a minimal /api/chat request with a single tool definition.
        If the model responds with a tool_call, it supports native tools.
        If it responds with plain text or errors, it doesn't.
        """
        test_tools = [{
            "type": "function",
            "function": {
                "name": "test__ping",
                "description": "Test function",
                "parameters": {
                    "type": "object",
                    "properties": {"msg": {"type": "string", "description": "message"}},
                    "required": [],
                },
            },
        }]
        try:
            payload = {
                "model": self.local_model,
                "messages": [{"role": "user", "content": "ping"}],
                "tools": test_tools,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 50},
            }
            with httpx.Client(timeout=10.0) as client:
                response = client.post(f"{self.local_base_url}/api/chat", json=payload)
            if response.status_code != 200:
                return False
            data = response.json()
            # Model supports tools if it returns a message with tool_calls field
            msg = data.get("message", {})
            return "tool_calls" in msg or isinstance(msg.get("tool_calls"), list)
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    # Streaming support for real-time token output
    # ──────────────────────────────────────────────────────────────────

    def stream_chat(self, text: str):
        """Generator that yields tokens from the LLM in real-time.

        Used by the web UI to show streaming responses instead of
        waiting for the full output. Only for conversational responses.
        """
        if not self._ready or self.llm_backend != "local_ollama":
            yield "No active LLM backend."
            return

        payload = {
            "model": self.local_model,
            "messages": [{"role": "user", "content": text}],
            "stream": True,
            "options": {"temperature": 0.7, "num_predict": 1000},
        }
        try:
            with httpx.Client(timeout=120.0) as client:
                with client.stream("POST", f"{self.local_base_url}/api/chat", json=payload) as response:
                    for line in response.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                yield token
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"\n[Stream error: {e}]"

    def update_commands(self, commands: List[Dict]):
        """Update the list of available commands (called after plugins load)."""
        self.available_commands = commands
        self._command_summary = self._build_command_summary()

        # Auto-detect native tool support on first command update
        if (self.use_native_tools
                and self._supports_native_tools is None
                and self._ready
                and self.llm_backend == "local_ollama"):
            self._supports_native_tools = self._detect_native_tool_support()
            if self._supports_native_tools:
                log.info("🔧 Native tool calling detected — using /api/chat with tools (faster, no JSON repair)")
            else:
                log.info("📝 Model does not support native tools — using strict JSON prompt mode")

    def _build_command_summary(self, commands: List[Dict] = None) -> str:
        """Build a concise summary of all available plugin commands.

        When `commands` is provided, summarizes only those commands (smart context).
        """
        cmds = commands or self.available_commands
        lines = []
        by_plugin = {}
        for cmd in cmds:
            plugin = cmd.get("plugin", "unknown")
            by_plugin.setdefault(plugin, []).append(cmd)

        for plugin, cmds in by_plugin.items():
            cmd_strs = []
            for c in cmds:
                name = c.get("name", "?")
                desc = c.get("description", "")
                usage = c.get("usage", "")
                cmd_strs.append(f"  - {plugin}.{name}: {desc}" + (f" (usage: {usage})" if usage else ""))
            lines.append(f"\n[{plugin}]")
            lines.extend(cmd_strs)

        return "\n".join(lines)

    def _make_system_prompt(self) -> str:
        """Build the system prompt for Gemini with all available commands."""
        return f"""You are the AI brain of "Autocrat", a Windows PC automation system.
The user gives natural language commands. Your job is to decide what action to take.

## Available Actions
{self._command_summary}

## Rules
1. If the user's request maps to one or more available actions, respond in STRICT JSON:
   {{"action": "plugin_name.command_name", "params": {{"query": "..."}}, "explanation": "brief reason"}}
   
2. For MULTI-STEP tasks, return a JSON array:
   [{{"action": "...", "params": {{}}}}, {{"action": "...", "params": {{}}}}]

3. If the command needs a "query" or "target" parameter, extract it from the user's message.

4. If no available action fits but you CAN answer the question directly (math, trivia, advice),
   respond as plain text — be helpful, concise, and smart.

5. If truly unsure, say so briefly and suggest what the user could try.

6. NEVER make up action names that aren't in the available list above.
7. For params, common keys are: query, target, title, path, direction, src, dst.
8. Keep explanations under 20 words.

Respond ONLY with the JSON or plain text answer — nothing else."""

    def _make_local_strict_json_prompt(self, user_text: str, commands: List[Dict] = None) -> str:
        """Very strict JSON prompt for local tool-calling models.

        When `commands` is provided, uses only those commands instead of the
        full summary (smart context window).
        """
        if commands:
            summary = self._build_command_summary(commands)
        else:
            summary = self._command_summary

        return f"""You are Autocrat tool router.

AVAILABLE ACTIONS:
{summary}

MANDATORY OUTPUT RULES:
1) Output ONLY valid JSON. No markdown. No backticks. No prose.
2) Use EXACT schema for single step:
   {{"action":"plugin.command","params":{{}}}}
3) Use EXACT schema for multi-step:
   [{{"action":"plugin.command","params":{{}}}},{{"action":"plugin.command","params":{{}}}}]
4) Action MUST be one from AVAILABLE ACTIONS.
5) params MUST be an object. Use extracted values from user text.
6) If unsure, choose the closest valid action; never invent action names.

USER COMMAND:
{user_text}
"""

    def _make_json_repair_prompt(self, user_text: str, previous_reply: str, parse_error: str) -> str:
        """Prompt to self-correct malformed/non-compliant JSON outputs."""
        return f"""Your previous response was invalid for strict tool-calling.

ERROR:
{parse_error}

ORIGINAL USER COMMAND:
{user_text}

YOUR PREVIOUS OUTPUT:
{previous_reply}

Now return ONLY corrected valid JSON using one of the allowed actions and exact schema:
Single: {{"action":"plugin.command","params":{{}}}}
Multi: [{{"action":"plugin.command","params":{{}}}}]
No markdown. No explanation. JSON only.
"""

    def _ollama_generate(self, prompt: str, temperature: float = 0.1, num_predict: int = 500) -> str:
        """Call Ollama generate API and return raw text."""
        payload = {
            "model": self.local_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
            },
        }
        with httpx.Client(timeout=60.0) as client:
            response = client.post(f"{self.local_base_url}/api/generate", json=payload)
        response.raise_for_status()
        return (response.json().get("response") or "").strip()

    def process(self, text: str) -> Optional[Dict]:
        """
        Try to understand an unrecognized command via:
          1. Learning memory (instant, no API call)
          2. Gemini LLM (smart, dynamic)
          3. Enhanced local heuristics (offline fallback)
          
        Returns dict with 'action', 'params', 'confidence', 'source'
        or a conversational response if it's a general question.
        """
        # ── Step 1: Check learning memory ──
        cached = self.memory.lookup(text)
        if cached:
            cached_action, cached_params = self._normalize_action(
                cached.get("action", ""),
                cached.get("params", {}),
            )
            valid_actions = {f"{c['plugin']}.{c['name']}" for c in self.available_commands}
            if cached_action in valid_actions:
                cached["action"] = cached_action
                cached["params"] = cached_params
                log.info(f"💾 Memory hit: '{text}' → {cached['action']} (used {cached.get('times_used', '?')}x)")
                return cached
            log.info(f"Ignoring stale memory mapping for '{text}': {cached.get('action')}")

        # ── Step 2: Selected LLM backend ──
        if self._ready:
            if self.llm_backend == "local_ollama":
                return self._ask_local_llm(text)
            if self.llm_backend == "gemini":
                return self._ask_gemini(text)

        # ── Step 3: No API key — try enhanced heuristics ──
        return self._enhanced_local(text)

    def _ask_local_llm(self, text: str) -> Optional[Dict]:
        """Ask local Ollama model to interpret the command.

        Uses native tool calling when supported (faster, no repair loop needed).
        Falls back to strict JSON prompt engineering for older models.
        """
        # ── Try native tool calling first (much faster path) ──
        if self.use_native_tools and self._supports_native_tools:
            try:
                # Smart context: filter to relevant commands only
                relevant = self._filter_relevant_commands(text, max_commands=25)
                log.info(f"📋 Smart context: {len(relevant)}/{len(self.available_commands)} commands for LLM")

                result = self._ollama_chat_with_tools(text, commands=relevant)
                if result:
                    action = result.get("action", "")
                    if action and action != "__conversation__":
                        self.memory.remember(text, action, result.get("params", {}), 0.90)
                        self.memory.log_conversation(text, f"tool_call:{action}", was_action=True, action=action)
                    elif action == "__conversation__":
                        self.memory.log_conversation(text, result.get("response", ""), was_action=False)
                    return result
            except Exception as e:
                log.warning(f"Native tool calling failed, falling back to JSON mode: {e}")

        # ── Fallback: strict JSON prompt engineering ──
        try:
            # Smart context: build a filtered command summary
            relevant = self._filter_relevant_commands(text, max_commands=30)
            log.info(f"📋 Smart context: {len(relevant)}/{len(self.available_commands)} commands for LLM")

            if self.strict_json_mode:
                prompt = self._make_local_strict_json_prompt(text, commands=relevant)
            else:
                prompt = f"{self._make_system_prompt()}\n\nUser command: {text}"

            reply = self._ollama_generate(prompt, temperature=0.05 if self.strict_json_mode else 0.1)
            log.info(f"🤖 Local LLM replied: {reply[:120]}...")

            result, parse_error = self._parse_llm_response(reply, text, return_error=True)
            if result:
                if result.get("action"):
                    self.memory.remember(text, result["action"], result.get("params", {}), 0.82)
                    self.memory.log_conversation(text, reply, was_action=True, action=result["action"])
                result["source"] = "local_ollama"
                return result

            # Auto-retry self-correction loop for malformed JSON / invalid action
            if self.strict_json_mode and self.local_retry_count > 0:
                current_reply = reply
                current_error = parse_error or "Invalid or non-compliant JSON output"
                for attempt in range(1, self.local_retry_count + 1):
                    repair_prompt = self._make_json_repair_prompt(text, current_reply, current_error)
                    repaired = self._ollama_generate(repair_prompt, temperature=0.0, num_predict=350)
                    log.info(f"🔁 Local LLM repair attempt {attempt}/{self.local_retry_count}: {repaired[:120]}...")
                    repaired_result, repaired_error = self._parse_llm_response(repaired, text, return_error=True)
                    if repaired_result:
                        if repaired_result.get("action"):
                            self.memory.remember(text, repaired_result["action"], repaired_result.get("params", {}), 0.84)
                            self.memory.log_conversation(text, repaired, was_action=True, action=repaired_result["action"])
                        repaired_result["source"] = "local_ollama_repair"
                        repaired_result["repair_attempts"] = attempt
                        return repaired_result
                    current_reply = repaired
                    current_error = repaired_error or current_error

            self.memory.log_conversation(text, reply, was_action=False)
            return {
                "action": "__conversation__",
                "response": reply,
                "confidence": 0.76,
                "source": "local_ollama",
            }
        except Exception as e:
            log.warning(f"Local LLM call failed: {e}")
            return self._enhanced_local(text)

    def _ask_gemini(self, text: str) -> Optional[Dict]:
        """Ask Gemini to interpret the command."""
        try:
            system_prompt = self._make_system_prompt()
            
            response = self.model.generate_content(
                [
                    {"role": "user", "parts": [{"text": system_prompt}]},
                    {"role": "model", "parts": [{"text": "Understood. I'll map commands to available actions or answer directly. Send me a command."}]},
                    {"role": "user", "parts": [{"text": text}]},
                ],
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 500,
                }
            )

            reply = response.text.strip()
            log.info(f"🤖 Gemini replied: {reply[:120]}...")

            # Try to parse as JSON (action mapping)
            result = self._parse_llm_response(reply, text)
            if result:
                # Remember for next time
                if result.get("action"):
                    self.memory.remember(text, result["action"], result.get("params", {}), 0.85)
                    self.memory.log_conversation(text, reply, was_action=True, action=result["action"])
                return result

            # It's a conversational / knowledge answer
            self.memory.log_conversation(text, reply, was_action=False)
            return {
                "action": "__conversation__",
                "response": reply,
                "confidence": 0.8,
                "source": "gemini_llm",
            }

        except Exception as e:
            log.warning(f"Gemini call failed: {e}")
            return self._enhanced_local(text)

    def _parse_llm_response(self, reply: str, original_text: str, return_error: bool = False):
        """Parse the LLM response — could be JSON action or plain text."""
        # Strip markdown code fences if present
        clean = reply.strip()
        parse_error = ""
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)

        # Try JSON parse
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError as e:
            parse_error = f"JSONDecodeError: {e.msg} at pos {e.pos}"
            # Try to extract JSON from mixed text — supports one level of nesting
            json_match = re.search(r'(\{(?:[^{}]|\{[^{}]*\})*\}|\[(?:[^\[\]]|\[[^\[\]]*\])*\])', clean, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                except json.JSONDecodeError as e2:
                    parse_error = f"JSONDecodeError (extracted): {e2.msg} at pos {e2.pos}"
                    return (None, parse_error) if return_error else None
            else:
                return (None, parse_error or "No JSON object/array found") if return_error else None

        # Handle compact tuple-style format:
        # ["plugin.command", {"param":"x"}, "optional explanation"]
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
            candidate_action = parsed[0]
            candidate_params = parsed[1] if len(parsed) > 1 and isinstance(parsed[1], dict) else {}
            candidate_action, candidate_params = self._normalize_action(candidate_action, candidate_params)
            valid_actions = {f"{c['plugin']}.{c['name']}" for c in self.available_commands}
            if candidate_action in valid_actions:
                result = {
                    "action": candidate_action,
                    "params": candidate_params,
                    "confidence": 0.78,
                    "source": "local_ollama",
                }
                return (result, "") if return_error else result
            parse_error = f"Invalid action '{candidate_action}'"

        # Handle single action
        if isinstance(parsed, dict) and "action" in parsed:
            action = parsed["action"]
            params = parsed.get("params", {})
            action, params = self._normalize_action(action, params)
            # Validate it's a real action
            valid_actions = {f"{c['plugin']}.{c['name']}" for c in self.available_commands}
            if action in valid_actions:
                result = {
                    "action": action,
                    "params": params,
                    "confidence": 0.82,
                    "source": "gemini_llm",
                    "explanation": parsed.get("explanation", ""),
                }
                return (result, "") if return_error else result
            else:
                log.warning(f"LLM suggested invalid action: {action}")
                return (None, f"Invalid action '{action}'") if return_error else None

        # Handle multi-step array
        if isinstance(parsed, list):
            valid_actions = {f"{c['plugin']}.{c['name']}" for c in self.available_commands}
            steps = []
            for step in parsed:
                if isinstance(step, dict):
                    step_action, step_params = self._normalize_action(step.get("action", ""), step.get("params", {}))
                else:
                    step_action, step_params = "", {}
                if step_action in valid_actions:
                    steps.append({
                        "action": step_action,
                        "params": step_params,
                    })
            if steps:
                result = {
                    "action": steps[0]["action"],
                    "params": steps[0]["params"],
                    "multi_step": steps,
                    "confidence": 0.78,
                    "source": "gemini_llm",
                }
                return (result, "") if return_error else result
            parse_error = "Multi-step JSON did not contain valid actions"

        return (None, parse_error or "Unrecognized response format") if return_error else None

    def _normalize_action(self, action: str, params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Map common near-miss tool names/params into valid NEXUS actions."""
        if not isinstance(action, str):
            return "", params if isinstance(params, dict) else {}

        params = params if isinstance(params, dict) else {}
        a = action.strip()

        alias_map = {
            "power_tools.lock_pc": "smart_actions.lock_pc",
            "smart_actions.open_tab": "smart_actions.switch_tab",
            "app_launcher.open_url": "smart_actions.open_website",
            "app_launcher.open_folder": "file_ops.open",
        }
        normalized = alias_map.get(a, a)

        # Param key normalization
        if normalized == "smart_actions.switch_tab":
            if "query" not in params:
                if "title" in params:
                    params["query"] = params.get("title")
                elif "name" in params:
                    params["query"] = params.get("name")

        if normalized == "smart_actions.open_website":
            if "query" not in params and "url" in params:
                params["query"] = params.get("url")

        if normalized == "file_ops.open":
            if "path" not in params and "query" in params:
                params["path"] = params.get("query")

        return normalized, params

    def _enhanced_local(self, text: str) -> Optional[Dict]:
        """Enhanced local heuristics when LLM is unavailable."""
        text_lower = text.lower().strip()
        text_words = set(text_lower.split())

        # Quick math detection
        if re.match(r'^[\d\s+\-*/^().%]+$', text_lower):
            return {
                "action": "intelligence.calculate",
                "params": {"query": text},
                "confidence": 0.9,
                "source": "local_heuristic",
            }

        # URL detection  
        if re.match(r'https?://', text_lower) or re.match(r'\w+\.\w+\.\w+', text_lower):
            return {
                "action": "smart_actions.open_website",
                "params": {"query": text},
                "confidence": 0.9,
                "source": "local_heuristic",
            }

        # "open <app>" pattern
        m = re.match(r"^open\s+(.+)$", text_lower)
        if m:
            return {
                "action": "app_launcher.open",
                "params": {"query": m.group(1)},
                "confidence": 0.7,
                "source": "local_heuristic",
            }

        # Keyword scan over available commands
        best_score = 0
        best_cmd = None
        for cmd in self.available_commands:
            cmd_words = set(cmd.get("description", "").lower().split())
            cmd_words.update(cmd.get("usage", "").lower().split())
            cmd_words.update(cmd.get("name", "").lower().replace("_", " ").split())
            if cmd_words and text_words:
                overlap = len(text_words & cmd_words) / max(len(text_words | cmd_words), 1)
                if overlap > best_score:
                    best_score = overlap
                    best_cmd = cmd

        if best_cmd and best_score > 0.3:
            return {
                "action": f"{best_cmd['plugin']}.{best_cmd['name']}",
                "params": {"query": text},
                "confidence": round(min(best_score + 0.2, 0.8), 3),
                "source": "local_keyword_match",
            }

        return None

    def chat(self, text: str) -> str:
        """Direct conversational chat with currently selected backend."""
        if not self._ready:
            return "💡 No active LLM backend. Start Ollama and pull the configured local model."

        if self.llm_backend == "local_ollama":
            try:
                payload = {
                    "model": self.local_model,
                    "prompt": text,
                    "stream": False,
                    "options": {"temperature": 0.7, "num_predict": 1000},
                }
                with httpx.Client(timeout=120.0) as client:
                    response = client.post(f"{self.local_base_url}/api/generate", json=payload)
                response.raise_for_status()
                reply = (response.json().get("response") or "").strip()
                self.memory.log_conversation(text, reply)
                return reply
            except Exception as e:
                return f"Error talking to local LLM: {e}"

        if self.llm_backend == "gemini":
            try:
                response = self.model.generate_content(
                    text,
                    generation_config={"temperature": 0.7, "max_output_tokens": 1000},
                )
                reply = response.text.strip()
                self.memory.log_conversation(text, reply)
                return reply
            except Exception as e:
                return f"Error talking to Gemini: {e}"

        return "No active backend"

    def get_stats(self) -> Dict:
        """Get AI engine stats."""
        memory_stats = self.memory.get_stats()
        return {
            "gemini_active": self._ready and self.llm_backend == "gemini",
            "llm_backend": self.llm_backend,
            "llm_active": self._ready,
            "model": self.local_model if self.llm_backend == "local_ollama" else ("gemini-2.0-flash" if self._ready else "none"),
            "native_tool_calling": bool(self.use_native_tools and self._supports_native_tools),
            "streaming_enabled": True,
            "smart_context_window": True,
            **memory_stats,
        }
