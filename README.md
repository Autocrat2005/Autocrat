<div align="center">

# ⚡ Autocrat

### Open-source JARVIS for your Windows PC.

One prompt. Full system control. It builds the tools it doesn't have.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Ollama](https://img.shields.io/badge/LLM-Ollama-FF6F00.svg)](https://ollama.com/)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg?logo=windows&logoColor=white)]()
[![Plugins](https://img.shields.io/badge/plugins-17%20built--in-22C55E.svg)]()
[![Commands](https://img.shields.io/badge/commands-160%2B-22C55E.svg)]()

<br>

<img src="screenshots/banner.png" alt="Autocrat — JARVIS for your PC" width="700">

<br>

[See It In Action](#-see-it-in-action) · [Quick Start](#-quick-start) · [How It Thinks](#-how-it-thinks) · [What It Can Do](#-what-it-can-do) · [Architecture](#-under-the-hood)

</div>

---

## 💬 See It In Action

> Autocrat understands you like an assistant, not a terminal.

### Just talk to it

```
You:       "close spotify, mute the volume, and take a screenshot"
Autocrat:   ✓ Killed process: Spotify.exe
            ✓ Volume muted
            ✓ Screenshot saved → screenshots/capture_20260301_143022.png
            ⏱ 340ms (3 actions, parallel)
```

### Ask it to build tools that don't exist yet

```
You:       "build a plugin that monitors my CPU temperature and alerts me when it crosses 80°C"
Autocrat:   🔨 Generating plugin: cpu_temp_monitor
            🔍 AST validation passed (no unsafe patterns)
            ✅ Plugin loaded — 2 new commands registered
            → Try: "check cpu temp" or "set temp alert 80"
```

### Let it browse the web for you

```
You:       "go to github.com/trending and tell me the top 3 repos today"
Autocrat:   🌐 Launching headless browser...
            📄 Navigating to github.com/trending
            🔍 Extracting trending repositories...

            1. google/gemma-3 — Lightweight open model by Google DeepMind
            2. ollama/ollama — Get up and running with large language models
            3. microsoft/TypeScript — TypeScript language and compiler
            ⏱ 2.4s
```

### Control your PC from your phone

```
You (from phone, via ngrok):   "lock my pc"
Autocrat:                       ⚠️ Destructive action: lock workstation
                                Approve? [yes/no]
You:                            "yes"
Autocrat:                       ✓ Workstation locked.
```

### Chain complex automations in plain English

```
You:       "create a folder called 'DailyReport', take a screenshot, save it there,
            then write a text file with today's CPU and RAM usage"
Autocrat:   ✓ Created folder: DailyReport
            ✓ Screenshot → DailyReport/screen_20260301.png
            ✓ System stats written → DailyReport/stats_20260301.txt
            ⏱ 580ms (workflow: 4 steps)
```

### Ask it anything — it's also a chatbot

```
You:       "explain the difference between threads and processes in python"
Autocrat:   A process is an independent program with its own memory space...
            [streams token-by-token with a blinking cursor]
```

---

## 🚀 Quick Start

```bash
# 1. Clone & install
git clone https://github.com/Autocrat2005/Autocrat.git
cd Autocrat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 2. Pull the brain
ollama pull qwen2.5-coder:3b
ollama serve                     # keep running in background

# 3. Launch your assistant
python main.py                   # CLI — talk in the terminal
python main.py --web             # Web dashboard — http://127.0.0.1:9000
```

### Access from anywhere

```bash
ngrok http 9000                  # tunnel it
# → Open the ngrok URL on your phone, tablet, or another PC
```

You now have a JARVIS-style AI controlling your desktop from any device on earth.

---

## 🧠 How It Thinks

### The 4-Stage Brain

Every input goes through four layers. The fastest one that understands you wins — the rest don't even fire.

```
 "open chrome"
      │
      ▼
 ┌──────────────────────────────────────────────────────────┐
 │  Stage 1: Regex Parser                          < 1ms    │
 │  200+ hand-tuned patterns. Instant recognition.          │
 │  "open chrome" → appLauncher.launch(name="chrome") ✅    │
 └──────────────────────────────────────────────────────────┘
      ↓ (only if Stage 1 didn't match)

 ┌──────────────────────────────────────────────────────────┐
 │  Stage 2: ML Brain                              ~ 5ms    │
 │  Sentence-transformer (all-MiniLM-L6-v2).                │
 │  Semantic similarity against learned intents.             │
 └──────────────────────────────────────────────────────────┘
      ↓ (only if Stage 2 confidence < threshold)

 ┌──────────────────────────────────────────────────────────┐
 │  Stage 3: LLM (Ollama)                          ~ 1-3s   │
 │  Smart context filter → 30 most relevant commands.       │
 │  Native tool calling → model picks function + params.    │
 │  "play music in my downloads folder" → complex mapping.  │
 └──────────────────────────────────────────────────────────┘
      ↓ (only if no action matched)

 ┌──────────────────────────────────────────────────────────┐
 │  Stage 4: Conversational                        ~ 1-3s   │
 │  General knowledge. "What's a mutex?" → answer streams   │
 │  token-by-token to the web UI via SSE.                   │
 └──────────────────────────────────────────────────────────┘
```

**90% of commands resolve in Stage 1 or 2 — under 10ms.** The LLM is a smart fallback, not the bottleneck.

### The Agentic Engine (v2.0)

When the LLM _does_ fire, it's not dumb prompt engineering. It's proper agent-style tool use:

```
Input: "close spotify and take a screenshot"
  │
  ├─ Smart Context Window
  │    160+ commands → keyword scoring + synonyms → top 30 relevant sent
  │
  ├─ Native Tool Calling (Ollama /api/chat)
  │    Model receives structured function definitions
  │    Returns: tool_call(processController.kill, {name: "spotify"})
  │           + tool_call(screenIntel.screenshot, {})
  │
  └─ Parallel Executor
       Different plugins → fire concurrently (ThreadPoolExecutor)
       Both finish in ~200ms instead of ~400ms sequentially
```

<details>
<summary><strong>v1.0 → v2.0 comparison (click to expand)</strong></summary>

|                   | v1.0 (Old)                                                                          | v2.0 (Current)                                                                    |
| ----------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **LLM ↔ Actions** | All 160+ commands in one prompt. LLM returns JSON. Malformed JSON → repair → retry. | Native tool calling via `/api/chat`. Model picks tools directly. No JSON hacking. |
| **Context**       | Entire catalog every time (~4K tokens wasted).                                      | Smart filter → only ~30 relevant commands. 80% fewer tokens.                      |
| **Multi-step**    | Sequential. 3 actions = 3x the time.                                                | Parallel. Independent actions run concurrently.                                   |
| **Web responses** | Spinner → wait → full text blob appears.                                            | Token-by-token streaming via SSE. Blinking cursor.                                |
| **Model compat**  | One prompting style. Switch model = rewrite.                                        | Auto-detects capabilities. Falls back gracefully.                                 |

</details>

---

## 🛠 What It Can Do

### 17 Built-In Plugins (160+ commands)

| Category          | Plugin            | Highlights                                                                                                     |
| ----------------- | ----------------- | -------------------------------------------------------------------------------------------------------------- |
| 🪟 **Desktop**    | windowManager     | Focus, minimize, maximize, resize, snap, tile windows                                                          |
|                   | appLauncher       | Open any app by name — "open chrome", "launch vscode"                                                          |
|                   | keyboardMouse     | Type text, hotkeys, mouse clicks, scroll, drag                                                                 |
| ⚙️ **System**     | processController | List, kill, monitor processes — "kill chrome", "top processes"                                                 |
|                   | systemInfo        | CPU, RAM, disk, battery, network stats, uptime                                                                 |
|                   | powerTools        | Shutdown, restart, sleep, hibernate, lock                                                                      |
|                   | volumeDisplay     | Volume up/down/mute, screen brightness                                                                         |
| 📁 **Files**      | fileOps           | Create, read, write, delete, search, move files & folders                                                      |
|                   | clipboard         | Copy, paste, clipboard history                                                                                 |
|                   | shellExecutor     | Run any shell command with captured output                                                                     |
| 🌐 **Web**        | cometWebAgent     | Headless Playwright browser — navigate, click, extract, screenshot. Uses a ReAct loop for multi-step browsing. |
| 🤖 **AI**         | coreBuilder       | **The meta-plugin.** Generates, validates, hot-loads, and auto-heals other plugins at runtime.                 |
|                   | intelligence      | Proactive nudges, context probes, system health monitoring                                                     |
|                   | smartActions      | Context-aware compound actions                                                                                 |
| 📋 **Automation** | workflowEngine    | Chain multi-step workflows. LLM can generate workflow YAML from plain English.                                 |
|                   | taskScheduler     | Schedule recurring commands (cron-style)                                                                       |
|                   | screenIntel       | Screenshots, OCR text extraction, screen region capture                                                        |

### Self-Writing Plugins (the JARVIS part)

This is the killer feature. **If a capability doesn't exist, Autocrat builds it on the spot.**

```
You: "build plugin that fetches current weather for any city"
```

What happens behind the scenes:

```
 1. LLM generates a full NexusPlugin subclass (Python file)
 2. AST Validator scans for safety:
    ✓ No eval/exec/os.system
    ✓ No ctypes/winreg
    ✓ Proper NexusPlugin structure
 3. Network Scanner detects URL: wttr.in
    ⚠️ Not in allowlist → Security Prompt:
    ┌─────────────────────────────────────────────────┐
    │  Plugin 'weather_fetcher' wants to reach wttr.in │
    │  [1] Allow Once  [2] Allow Always  [3] Block    │
    └─────────────────────────────────────────────────┘
 4. You pick "Allow Always" → domain saved to config
 5. Plugin is importlib-loaded into the live engine
 6. New commands registered immediately — no restart
```

Now you can say:

```
You:       "weather in Mumbai"
Autocrat:   🌍 Mumbai, India:
            🌡️  30°C (86°F) — feels like 34°C
            ☁️  Smoke
            💧 Humidity: 43%
            💨 Wind: 21 km/h WNW
```

If the generated plugin **crashes at runtime**, the error traceback is sent back to the LLM, which patches the code and reloads it automatically.

### Web Dashboard (your control center)

Start with `python main.py --web` and open `http://127.0.0.1:9000`:

- **Live terminal** — type commands, get streamed responses, click autocomplete suggestions
- **System gauges** — real-time CPU, RAM, disk, battery with animated arcs
- **Plugin explorer** — browse all plugins, see every command, click to auto-fill
- **Command history** — searchable log of everything you've run
- **Workflow builder** — create and trigger multi-step automations
- **Confirmation alerts** — destructive actions trigger a WebSocket popup for approval

Tunnel it with ngrok and you have a **remote AI assistant for your PC** accessible from any device.

---

## 🔒 Safety

Nothing runs unless validated. Three layers of defense:

| Layer                       | Scope             | What it does                                                                                                                                                        |
| --------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **AST Sandbox**             | Generated plugins | Parses code before execution. Blocks `eval`, `exec`, `ctypes`, `winreg`, `os.system`, `subprocess.Popen`, `shutil.rmtree`. Verifies proper `NexusPlugin` structure. |
| **Network Permissions**     | Generated plugins | Scans every URL/domain in code. Unapproved domains trigger Allow Once / Allow Always / Block prompt. No silent network access.                                      |
| **Destructive Action Gate** | All plugins       | `shutdown`, `restart`, `kill`, `delete` require explicit approval. Alert pushed to all clients (web, Telegram, VS Code).                                            |

> Think of it like Android permissions, but for your desktop AI.

---

## 🏗 Under The Hood

### Folder Structure

```
Autocrat/
├── main.py                     # Entry point (CLI + web server)
├── nexus_config.yaml           # Master configuration
├── requirements.txt            # Dependencies
│
├── nexus/
│   ├── core/
│   │   ├── engine.py           # Command router + parallel executor
│   │   ├── ai_engine.py        # LLM integration (native tools + streaming)
│   │   ├── brain.py            # ML intent classifier (sentence-transformers)
│   │   ├── parser.py           # Regex command parser (200+ patterns)
│   │   ├── config.py           # YAML config manager
│   │   ├── events.py           # Event bus for cross-plugin communication
│   │   ├── learner.py          # Behavioral learning (time, chain, frequency)
│   │   ├── logger.py           # Structured logging
│   │   └── plugin.py           # Base plugin class
│   │
│   ├── plugins/
│   │   ├── core_builder.py     # Meta-plugin: generates other plugins
│   │   ├── comet_web_agent.py  # Headless browser (Playwright + ReAct)
│   │   ├── workflow_engine.py  # Multi-step workflow orchestration
│   │   ├── generated/          # Auto-generated plugins land here
│   │   └── ...                 # 14 more built-in plugins
│   │
│   ├── integrations/
│   │   └── telegram_bot.py     # Telegram remote control
│   │
│   └── web/
│       ├── server.py           # FastAPI + SSE streaming + WebSocket
│       └── static/             # Web dashboard (HTML / CSS / JS)
│
├── workflows/                  # Saved workflow YAML files
├── logs/                       # Runtime logs
└── screenshots/                # Captured screens
```

### Configuration

```yaml
ai:
  llm_backend: local_ollama
  local_model: qwen2.5-coder:3b
  local_base_url: http://127.0.0.1:11434
  use_native_tools: true # native Ollama tool calling
  strict_json_mode: true # JSON fallback for older models

system:
  safe_mode: false

safety:
  confirm_destructive: true
  web:
    allowlist_domains:
      - github.com
      - codeforces.com
      - wttr.in
      - localhost
```

Domains get added automatically when you approve "Allow Always" through the security prompt.

### Requirements

| What                          | Why                                    |
| ----------------------------- | -------------------------------------- |
| Python 3.10+                  | Modern syntax, type hints              |
| [Ollama](https://ollama.com/) | Local LLM (qwen2.5-coder:3b)           |
| Windows 10/11                 | Win32 system automation APIs           |
| ~2GB RAM                      | Sentence-transformer + Ollama overhead |

Key packages: `fastapi` · `uvicorn` · `httpx` · `sentence-transformers` · `playwright` · `psutil` · `pyautogui` · `pycaw` · `google-generativeai` (optional)

### Why not LangChain?

- **+50MB deps** for stuff Autocrat already does natively
- **Opaque wrappers** — a tool call fails 4 layers deep, good luck debugging
- **Ollama already has a tool-calling API** — wrapping it again adds latency, not features

We built the four things that actually matter (native tools, smart filtering, parallel exec, streaming) in ~500 lines. Zero new dependencies.

---

## 🗺 Roadmap

- [x] Native LLM tool calling (Ollama `/api/chat`)
- [x] Smart context window (keyword-relevance filtering)
- [x] Parallel multi-step execution
- [x] Streaming responses (SSE)
- [x] Auto-healing generated plugins
- [x] Dynamic network permissions
- [ ] 🎤 Voice control (faster-whisper — "Hey Autocrat, lock my PC")
- [ ] 🐧 Linux / macOS support (replace Win32 APIs)
- [ ] 🏪 Plugin marketplace (share generated plugins with others)
- [ ] 🤖 Multi-agent mode (agents that spawn sub-agents)
- [ ] 💻 VS Code extension (run commands inline)
- [ ] 🧠 Persistent memory (remember preferences across sessions)

---

## 🤝 Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📄 License

MIT — See [LICENSE](LICENSE).

---

<div align="center">

**Built by [@Autocrat2005](https://github.com/Autocrat2005)**

If this project is useful, consider giving it a ⭐

_"Sir, I've prepared a flight plan..."_ — Well, not yet. But we're getting there.

</div>
