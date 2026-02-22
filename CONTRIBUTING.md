# Contributing to Autocrat CLI

Thanks for your interest in contributing! Autocrat is an open-source desktop automation OS and we welcome pull requests.

## Getting Started

1. **Fork** the repo and clone it locally
2. Create a virtual environment and install deps:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Pull the LLM model:
   ```bash
   ollama pull qwen2.5-coder:3b
   ```
4. Run the app to make sure everything works:
   ```bash
   python main.py --web
   ```

## Project Structure

- `nexus/core/` — Engine, parser, brain, config, events, logging
- `nexus/plugins/` — All built-in plugins + `generated/` for auto-created ones
- `nexus/plugins/core_builder.py` — The meta-plugin that generates other plugins
- `nexus/web/` — FastAPI server + static dashboard
- `nexus/integrations/` — Telegram bot integration
- `main.py` — Entry point

## How to Add a New Plugin

Create a file in `nexus/plugins/` that subclasses `NexusPlugin`:

```python
from nexus.core.plugin import NexusPlugin

class MyPlugin(NexusPlugin):
    name = "my_plugin"
    description = "Does something cool"
    version = "1.0.0"
    icon = "🔧"

    def setup(self):
        self.register_command(
            "my_command", self.my_command,
            "Description of what it does",
            "my_command <arg>",
            aliases=["mc"],
            keywords=["my", "command"],
        )

    def my_command(self, arg='', **kwargs):
        return {"success": True, "result": f"Did something with {arg}"}
```

Then add the plugin name to `nexus_config.yaml` under `plugins.enabled`.

## Adding Parser Patterns

If you want your plugin to be matched by the regex parser (fastest path), add patterns in `nexus/core/parser.py` under the appropriate section.

## Code Style

- Python 3.10+
- Use type hints where practical
- Follow existing patterns — consistency matters more than perfection
- Every plugin method should return `{"success": bool, "result": ...}` or `{"success": False, "error": "..."}`

## Pull Request Checklist

- [ ] New plugin follows the `NexusPlugin` base class pattern
- [ ] Parser patterns added (if applicable)
- [ ] No hardcoded API keys or secrets
- [ ] Tested locally with `python main.py --web`
- [ ] Destructive actions (delete, kill, shutdown) require confirmation

## Reporting Bugs

Open an issue with:
- What you did
- What you expected
- What happened instead
- Your OS and Python version

## Feature Requests

Open an issue with the `enhancement` label and describe the use case.

---

Thanks for helping make Autocrat better.
