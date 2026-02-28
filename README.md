<div align="center">

# ⚡ Autocrat

**Your PC, on autopilot.**

An AI-powered desktop automation OS that understands natural language, writes its own plugins at runtime, and asks before it touches the internet.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Ollama](https://img.shields.io/badge/LLM-Ollama-FF6F00.svg)](https://ollama.com/)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg?logo=windows&logoColor=white)]()
[![Plugins](https://img.shields.io/badge/plugins-17%20built--in-22C55E.svg)]()
[![Commands](https://img.shields.io/badge/commands-160%2B-22C55E.svg)]()

<br>

<img src="screenshots/banner.png" alt="Autocrat Banner" width="680">

<br>

[Quick Start](#-quick-start) · [Features](#-features) · [Usage](#-usage-examples) · [Architecture](#-architecture) · [Config](#%EF%B8%8F-configuration) · [Contributing](#-contributing)

</div>

---

## 🧠 The Idea

Most automation tools make you learn their scripting language. Autocrat flips that — **you describe what you want in plain English, and it figures out the rest.**

It's not just a command runner. It's a self-extending system: if a capability doesn't exist yet, the local LLM generates a validated plugin on the fly, loads it without restarting, and if the new code tries to hit an unapproved domain, the system pauses and asks you — like Android asking for camera permissions.

> **TL;DR** — Tell your computer what to do. It does it. If it can't, it builds the tool first.

---

## 🚀 Quick Start

```bash
# 1. Clone & set up
git clone https://github.com/Autocrat2005/Autocrat.git
cd Autocrat
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 2. Pull the LLM
ollama pull qwen2.5-coder:3b
ollama serve                     # keep this running

# 3. Launch
python main.py                   # CLI mode
python main.py --web             # web dashboard on http://127.0.0.1:9000
```

Control it from your phone by tunneling with [ngrok](ngrok_setup.md):

```bash
ngrok http 9000   # → share the URL, open on any device
```

---

## ✨ Features

### Self-Writing Plugins

Describe what you need. Autocrat generates the code, validates it through an AST sandbox, and hot-loads it — no restart.

```
> build plugin that batch renames files in a folder by adding a prefix and timestamp
```

If the generated code tries to reach an external API, the system pauses:

```
⚠️  Plugin 'weather_fetcher' requests network access to: wttr.in

  [1] Allow Once       — this session only
  [2] Allow Always     — persist to config
  [3] Block & Delete   — remove the plugin
```

No unauthorized network calls. Ever.

### Native Tool Calling

Instead of wrestling with prompt-engineered JSON, Autocrat uses **Ollama's native `/api/chat` tool-calling protocol**. Each of the 160+ commands is registered as a structured tool definition — the model picks the right function and parameters directly.

Falls back to strict JSON prompting automatically if the model doesn't support tools.

### Smart Context Window

With 160+ available commands, sending everything to the LLM wastes tokens and confuses smaller models. Autocrat's **keyword-relevance filter** scores commands against the user's input using synonym expansion, then sends only the top ~30 most relevant commands to the LLM.

Result: faster responses, higher accuracy, lower token usage.

### Parallel Execution

Multi-step commands that target different plugins run **concurrently** via a thread pool. Independent actions like "minimize all windows, mute volume, and take a screenshot" execute in parallel instead of sequentially.

### Streaming Responses

Conversational AI answers stream **token-by-token** to the web dashboard via Server-Sent Events (SSE). You see the response forming in real-time with a blinking cursor — no more staring at a spinner waiting for the full reply.

### 4-Stage AI Pipeline

Every command flows through four stages — the first one that matches wins:

| Stage | Engine         | Speed | What it does                                         |
| ----- | -------------- | ----- | ---------------------------------------------------- |
| 1     | Regex parser   | <1ms  | Exact pattern matching (200+ patterns)               |
| 2     | ML Brain       | ~5ms  | Sentence-transformer embeddings (`all-MiniLM-L6-v2`) |
| 3     | Local LLM      | ~1-3s | Ollama with native tool calling or structured JSON   |
| 4     | Conversational | ~1-3s | General knowledge answers when no action matches     |

### Remote Control

Start the web server, tunnel it with ngrok, and control your PC from any browser — phone, tablet, another machine. The dashboard includes:

- Live terminal with autocomplete
- System dashboard (CPU / RAM / disk gauges)
- Plugin explorer with clickable commands
- Command history
- Workflow builder

### 17 Built-In Plugins

<details>
<summary><strong>Click to expand full plugin list</strong></summary>

| Plugin                | What it does                                                       |
| --------------------- | ------------------------------------------------------------------ |
| **windowManager**     | Focus, minimize, maximize, resize, snap windows                    |
| **processController** | List, kill, monitor processes                                      |
| **fileOps**           | Create, read, write, delete, search files and folders              |
| **keyboardMouse**     | Type text, hotkeys, mouse clicks, scrolling                        |
| **screenIntel**       | Screenshots, OCR, screen region capture                            |
| **appLauncher**       | Open apps by name                                                  |
| **clipboard**         | Copy, paste, clipboard history                                     |
| **systemInfo**        | CPU, RAM, disk, battery, network stats                             |
| **volumeDisplay**     | Volume up/down/mute, display brightness                            |
| **shellExecutor**     | Run shell commands with output capture                             |
| **taskScheduler**     | Schedule recurring commands (cron-style)                           |
| **workflowEngine**    | Chain multi-step workflows, LLM-generated workflows                |
| **smartActions**      | Context-aware compound actions                                     |
| **powerTools**        | Shutdown, restart, sleep, hibernate, lock                          |
| **intelligence**      | Proactive nudges, context probes, system health                    |
| **cometWebAgent**     | Headless browser automation with Playwright (ReAct loop)           |
| **coreBuilder**       | Meta-plugin — generates, validates, loads, and heals other plugins |

</details>

### Safety Architecture

- **Blocked imports**: `ctypes`, `winreg`, raw `socket`
- **Blocked functions**: `eval`, `exec`, `os.system`, `subprocess.Popen`, `shutil.rmtree`
- **Destructive action confirmation**: shutdown, restart, kill, delete all require explicit approval before execution
- **Domain allowlist**: web navigation and API calls only to pre-approved domains
- **Dynamic permission prompts**: generated plugins that reach out to new domains trigger an interactive allow/block prompt

---

## 💡 Usage Examples

### Generate a plugin from your phone

You're on the bus. You need a batch-rename tool for tonight. Pull out your phone, hit the ngrok URL:

```
build plugin that batch renames all files in a given folder by adding a prefix and timestamp
```

By the time you reach your desk, the plugin is generated, validated, and loaded.

### Build plugins that hit external APIs

```
build plugin that fetches current weather for a given city and returns temperature humidity and description
```

The system generates the plugin, detects `wttr.in` in the code, and pauses for permission. You approve, and:

```json
{
  "success": true,
  "result": "🌍 Weather in Ballard Estate, India:\n  🌡️  30°C (86°F) — feels like 34°C\n  ☁️  Smoke\n  💧 Humidity: 43%\n  💨 Wind: 21 km/h WNW"
}
```

### Scaffold an entire project remotely

```
workflow generate create a python project folder called myAutoProject with an init file, a main.py that prints hello world, and a requirements.txt
```

### Headless web scraping

```
react plan Go to codeforces.com/contests, find the most recent Div 2 contest, extract the contest name and id
```

The `cometWebAgent` launches headless Playwright, navigates, extracts data, and returns structured results.

### Chain complex automations

```
workflow generate extract the top trending repo from github.com/trending, then create a folder called trendingProject and write a README.md with the repo name and description
```

---

## 🏗 Architecture

### How Dynamic Permissions Work

```
User → "build plugin that fetches crypto prices from coinbase API"
         │
         ▼
  ┌─────────────────┐
  │  LLM generates   │
  │  plugin code      │
  └────────┬─────────┘
           │
           ▼
  ┌─────────────────┐
  │  AST Validator    │  → syntax check
  │                   │  → safety scan (blocked patterns)
  │                   │  → structure verify (NexusPlugin subclass)
  └────────┬─────────┘
           │
           ▼
  ┌─────────────────────┐
  │  Network URL Scanner  │  → detects "api.coinbase.com"
  │                       │  → not in allowlist!
  └────────┬─────────────┘
           │
           ▼
  ┌────────────────────────────────────────┐
  │  ⚠️  SECURITY PROMPT                    │
  │  Plugin wants access to api.coinbase.com│
  │                                         │
  │  [1] Allow Once                         │
  │  [2] Allow Always  (persist to YAML)    │
  │  [3] Block & Delete                     │
  └────────┬───────────────────────────────┘
           │ (user picks 2)
           ▼
  ┌─────────────────┐
  │  Hot-load into    │  → importlib dynamic import
  │  live engine      │  → register commands
  └─────────────────┘
           │
           ▼
      ✅ Plugin ready
```

### Folder Structure

```
Autocrat/
├── main.py                     # Entry point (CLI + web server)
├── nexus_config.yaml           # Master configuration
├── requirements.txt            # Python dependencies
├── pyproject.toml              # Packaging config
│
├── nexus/
│   ├── core/
│   │   ├── engine.py           # Central command router + parallel executor
│   │   ├── ai_engine.py        # LLM integration (native tools + streaming)
│   │   ├── brain.py            # ML intent classifier (sentence-transformers)
│   │   ├── parser.py           # Regex command parser (200+ patterns)
│   │   ├── config.py           # YAML config manager
│   │   ├── events.py           # Event bus for plugin communication
│   │   ├── learner.py          # Behavioral learning (time, chain, frequency)
│   │   ├── logger.py           # Structured logging
│   │   └── plugin.py           # Base plugin class
│   │
│   ├── plugins/
│   │   ├── core_builder.py     # Meta-plugin: generates other plugins
│   │   ├── comet_web_agent.py  # Headless browser automation (Playwright)
│   │   ├── workflow_engine.py  # Multi-step workflow orchestration
│   │   ├── generated/          # Auto-generated plugins live here
│   │   └── ...                 # 14 more built-in plugins
│   │
│   ├── integrations/
│   │   └── telegram_bot.py     # Telegram remote control
│   │
│   └── web/
│       ├── server.py           # FastAPI + SSE streaming + WebSocket
│       └── static/             # Web dashboard (HTML/CSS/JS)
│
├── workflows/                  # Saved workflow YAML files
├── logs/                       # Runtime logs
└── screenshots/                # Screen captures
```

---

## ⚙️ Configuration

All settings live in `nexus_config.yaml`:

```yaml
ai:
  llm_backend: local_ollama
  local_model: qwen2.5-coder:3b
  local_base_url: http://127.0.0.1:11434
  use_native_tools: true # native Ollama tool calling
  strict_json_mode: true # fallback JSON mode

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

Domains are added automatically when you choose **"Allow Always"** through the dynamic permission prompt — no manual YAML editing needed.

---

## 📊 Requirements

| Dependency                    | Why                                         |
| ----------------------------- | ------------------------------------------- |
| Python 3.10+                  | f-strings, match statements, type hints     |
| [Ollama](https://ollama.com/) | Local LLM inference (qwen2.5-coder:3b)      |
| Windows 10/11                 | Win32 APIs for system automation            |
| ~2GB RAM                      | For the sentence-transformer + Ollama model |

Key Python packages: `fastapi`, `uvicorn`, `httpx`, `sentence-transformers`, `playwright`, `psutil`, `pyautogui`, `pycaw`, `google-generativeai` (optional Gemini cloud backend).

---

## 🤝 Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding plugins, parser patterns, and submitting PRs.

## 🗺 Roadmap

- [x] Native LLM tool calling (Ollama `/api/chat`)
- [x] Smart context window filtering
- [x] Parallel multi-step execution
- [x] Streaming responses (SSE)
- [ ] Linux / macOS support (replace Win32 APIs)
- [ ] Voice control via faster-whisper
- [ ] Plugin marketplace (share generated plugins)
- [ ] Multi-agent orchestration (agents spawning sub-agents)
- [ ] VS Code extension for inline command execution
- [ ] Persistent memory / context across sessions

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

**Built by [@Autocrat2005](https://github.com/Autocrat2005)**

If this project is useful, consider giving it a ⭐

</div>
