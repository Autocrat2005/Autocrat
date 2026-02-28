"""
Autocrat — Workflow Engine Plugin
Chain multiple commands into named, reusable, saveable workflows.
Supports cross-domain dynamic context so data flows between steps.

Context variables in command strings use {{var}} syntax:
  - {{prev_result}}      → result/result_data from previous step
  - {{prev_data}}        → result_data specifically (Comet extractions, etc.)
  - {{prev_url}}         → final_url from previous step
  - {{step_N_result}}    → result from step N
  - {{step_N_data}}      → result_data from step N
  - {{<plugin>_result}}  → result from most recent step that hit <plugin>
  - {{<plugin>_data}}    → result_data from most recent step that hit <plugin>
  - {{init.<key>}}       → initial variables passed to run_dynamic
"""

import json
import os
import re
import yaml
import time
from typing import Any, Dict, List, Optional
from nexus.core.plugin import NexusPlugin
from nexus.core.logger import get_logger

log = get_logger("workflow")


class WorkflowEnginePlugin(NexusPlugin):
    name = "workflow_engine"
    description = "Create, save, and run multi-step command workflows"
    icon = "🔗"

    def setup(self):
        self._engine = None
        self._recording: Dict[str, Any] = {}  # Current recording session
        self.workflows_dir = "workflows"
        os.makedirs(self.workflows_dir, exist_ok=True)

        self.register_command("create", self.create_workflow,
                              "Start creating a new workflow", "workflow create <name>")
        self.register_command("add_step", self.add_step,
                              "Add a command step to the current workflow", "workflow add <command>")
        self.register_command("save", self.save_workflow,
                              "Save the current workflow", "workflow save")
        self.register_command("run", self.run_workflow,
                              "Execute a saved workflow", "workflow run <name>")
        self.register_command("run_dynamic", self.run_dynamic,
                              "Run a workflow with initial context variables",
                              "workflow run_dynamic <name> key=value key2=value2")
        self.register_command("generate", self.generate_workflow,
                              "LLM generates & runs a workflow from a natural language goal",
                              "workflow generate <description of what you want>")
        self.register_command("list", self.list_workflows,
                              "List all saved workflows", "workflow list")
        self.register_command("delete", self.delete_workflow,
                              "Delete a saved workflow", "workflow delete <name>")

    def set_engine(self, engine):
        self._engine = engine

    def create_workflow(self, name: str = "", **kwargs):
        if not name:
            return {"success": False, "error": "Workflow name required"}

        self._recording = {
            "name": name,
            "steps": [],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return {
            "success": True,
            "result": f"📝 Recording workflow: '{name}'. Use 'workflow add <command>' to add steps, 'workflow save' to finish.",
        }

    def add_step(self, command: str = "", **kwargs):
        if not self._recording:
            return {"success": False, "error": "No workflow being recorded. Use 'workflow create <name>' first."}
        if not command:
            return {"success": False, "error": "Command required"}

        self._recording["steps"].append({
            "command": command,
            "delay": 0,  # Optional delay between steps
        })

        step_num = len(self._recording["steps"])
        return {
            "success": True,
            "result": f"Step {step_num} added: '{command}'",
            "total_steps": step_num,
        }

    def save_workflow(self, **kwargs):
        if not self._recording:
            return {"success": False, "error": "No workflow being recorded"}

        name = self._recording["name"]
        filename = f"{name.replace(' ', '_').lower()}.yaml"
        filepath = os.path.join(self.workflows_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                yaml.dump(self._recording, f, default_flow_style=False)

            steps = len(self._recording["steps"])
            self._recording = {}

            return {
                "success": True,
                "result": f"✅ Workflow '{name}' saved with {steps} steps → {filepath}",
                "path": filepath,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def run_workflow(self, name: str = "", **kwargs):
        """Execute a saved workflow with full cross-domain dynamic context."""
        return self._run_with_context(name, init_vars=kwargs)

    def run_dynamic(self, name: str = "", **kwargs):
        """Run a workflow with explicit initial context variables.

        Usage: workflow run_dynamic <name> key=value key2=value2
        All kwargs are injected as {{init.<key>}} variables.
        """
        return self._run_with_context(name, init_vars=kwargs)

    # ── Autonomous Workflow Generation (No-Code Jarvis) ─────────────────

    def generate_workflow(self, description: str = "", **kwargs):
        """LLM generates a workflow YAML on-the-fly from a natural language goal,
        auto-saves it, and immediately executes it."""
        if not description:
            return {
                "success": False,
                "error": "Describe what you want, e.g.: workflow generate set up a C++ contest workspace from codeforces",
            }
        if not self._engine:
            return {"success": False, "error": "Engine not connected"}

        # ── Access the LLM ──
        gemini = getattr(self._engine, "gemini", None)
        if not gemini or not gemini._ready:
            return {"success": False, "error": "LLM not available — start Ollama first"}

        prompt = self._build_generation_prompt(description)
        log.info(f"🧠 Generating workflow for: {description}")

        try:
            raw = gemini._ollama_generate(prompt, temperature=0.15, num_predict=1500)
        except Exception as e:
            return {"success": False, "error": f"LLM generation failed: {e}"}

        log.info(f"🧠 LLM raw output ({len(raw)} chars): {raw[:300]}...")

        # ── Parse the generated YAML/JSON ──
        workflow = self._parse_generated_workflow(raw, description)
        if not workflow:
            return {
                "success": False,
                "error": "LLM output could not be parsed into a valid workflow",
                "raw_output": raw[:800],
            }

        steps = workflow.get("steps", [])
        if not steps:
            return {
                "success": False,
                "error": "Generated workflow has no steps",
                "raw_output": raw[:800],
            }

        # ── Auto-save to disk for reuse ──
        saved_path = self._save_generated(workflow)
        log.info(f"💾 Saved generated workflow → {saved_path}")

        # ── Execute in-memory ──
        wf_name = workflow.get("name", description[:40])
        log.info(f"🚀 Executing generated workflow '{wf_name}' ({len(steps)} steps)")
        exec_result = self._run_with_context(
            wf_name, init_vars=kwargs, workflow_data=workflow
        )

        return {
            "success": exec_result.get("success", False),
            "result": {
                "mode": "autonomous_generation",
                "generated_workflow": {
                    "name": wf_name,
                    "steps": len(steps),
                    "saved_to": saved_path,
                },
                "execution": exec_result.get("result", exec_result.get("error", "")),
            },
        }

    def _build_generation_prompt(self, description: str) -> str:
        """Construct the LLM prompt that teaches the model how to write workflow YAML."""
        return f"""You are Autocrat workflow generator.
Given a user goal, produce a multi-step YAML workflow.

AVAILABLE COMMANDS:
1. react plan <task> start_url <url>
   Web scraping. Extracts data. MUST be first step when goal needs web info.
2. powershell <script>
   File/directory ops on Windows.
3. open <app>
   Launch an app.

CONTEXT VARIABLES (use in steps AFTER the step that produced them):
- {{{{comet_data}}}} = data from the react plan step
- {{{{comet_url}}}}  = URL visited by react plan
- {{{{prev_result}}}} = output of the previous step

EXAMPLE — user says "get top trending github repo and make a readme":
name: Trending Repo Readme
description: Extract trending repo then create README
steps:
  - command: "react plan Go to github.com/trending, extract the number 1 trending repo name and description, call extract_and_finish start_url github.com/trending"
    on_fail: abort
  - command: "powershell New-Item -ItemType Directory -Force -Path trending_project | Out-Null; 'Dir created'"
    on_fail: abort
  - command: "powershell $ErrorActionPreference='Stop'; Set-Content -Path trending_project/README.md -Value '# Trending Repo\n{{{{comet_data}}}}'; 'README written'"

RULES:
1. If the goal needs web data, step 1 MUST be react plan with start_url.
2. Use powershell for ALL file/dir ops. Add $ErrorActionPreference='Stop' for writes.
3. Reference web data with {{{{comet_data}}}} in LATER steps.
4. Wrap {{{{comet_data}}}} in single quotes inside powershell commands.
5. Use double-quoted command strings in YAML (command: "...").
6. Add on_fail: abort on critical steps.
7. Use FORWARD SLASHES in file paths (my_folder/file.txt), never backslashes.
8. Output ONLY valid YAML. No markdown fences. No explanation.

USER GOAL:
{description}
"""

    def _parse_generated_workflow(self, raw: str, description: str) -> Optional[Dict]:
        """Parse LLM output into a workflow dict. Handles YAML, JSON, and code-fenced output."""
        text = raw.strip()

        # Strip markdown code fences if present
        text = re.sub(r"^```(?:ya?ml|json)?\s*\n?", "", text)
        text = re.sub(r"\n?\s*```\s*$", "", text)
        text = text.strip()

        # ── Attempt 1: raw YAML parse ──
        result = self._try_yaml_parse(text, description)
        if result:
            return result

        # ── Attempt 2: sanitize YAML-hostile backslash paths ──
        # Inside double-quoted YAML strings, \m \_ \r etc. are invalid escapes.
        # Fix: in double-quoted values, escape lone backslashes to \\  OR replace with /
        sanitized = self._sanitize_yaml_backslashes(text)
        result = self._try_yaml_parse(sanitized, description)
        if result:
            return result

        # ── Attempt 3: replace ALL backslashes with forward slashes (PowerShell accepts both) ──
        fwd_slash = text.replace("\\", "/")
        result = self._try_yaml_parse(fwd_slash, description)
        if result:
            return result

        # ── Attempt 4: extract individual command lines (last resort) ──
        commands = re.findall(r"command:\s*[\"']?(.+?)[\"']?\s*$", text, re.MULTILINE)
        if commands:
            return self._normalize_generated(
                {"steps": [{"command": c.strip('"\' ')} for c in commands]},
                description,
            )

        return None

    def _try_yaml_parse(self, text: str, description: str) -> Optional[Dict]:
        """Try YAML, then JSON, then YAML-from-mixed-prose."""
        # YAML
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and "steps" in data:
                return self._normalize_generated(data, description)
        except yaml.YAMLError:
            pass

        # JSON
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "steps" in data:
                return self._normalize_generated(data, description)
        except (json.JSONDecodeError, ValueError):
            pass

        # Extract YAML block embedded in prose
        yaml_match = re.search(
            r"(name:\s*.+?\n(?:description:.*?\n)?steps:\s*\n(?:[ \t]+-\s+command:.+?\n?)+)",
            text, re.DOTALL,
        )
        if yaml_match:
            try:
                data = yaml.safe_load(yaml_match.group(1))
                if isinstance(data, dict) and "steps" in data:
                    return self._normalize_generated(data, description)
            except yaml.YAMLError:
                pass
        return None

    @staticmethod
    def _sanitize_yaml_backslashes(text: str) -> str:
        """Fix backslash paths inside double-quoted YAML strings.

        YAML treats \\x as an escape inside double quotes. LLMs often
        produce Windows paths like my_folder\\\\main.py which are invalid.
        We double-escape backslashes that precede non-YAML-escape chars.
        """
        # YAML recognized escape chars after backslash inside double quotes
        yaml_escapes = set('0abtnvfre "\\/N_LPxuU ')

        def fix_dq_string(m):
            s = m.group(0)
            fixed = []
            i = 0
            while i < len(s):
                if s[i] == '\\' and i + 1 < len(s):
                    nxt = s[i + 1]
                    if nxt not in yaml_escapes:
                        fixed.append('\\\\')  # double-escape
                        i += 1
                        continue
                fixed.append(s[i])
                i += 1
            return ''.join(fixed)

        # Match double-quoted strings in YAML (after command: "...")
        return re.sub(r'"[^"]*"', fix_dq_string, text)

        return None

    def _normalize_generated(self, data: Dict, description: str) -> Dict:
        """Ensure required fields exist on a generated workflow dict."""
        if not data.get("name"):
            # Derive name from description
            slug = re.sub(r"[^\w\s]", "", description)[:40].strip()
            data["name"] = slug or "Generated Workflow"
        if not data.get("description"):
            data["description"] = f"Auto-generated: {description}"
        data["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["source"] = "llm_generated"
        # Ensure each step has the right shape
        clean_steps = []
        for step in data.get("steps", []):
            if isinstance(step, str):
                step = {"command": step}
            if isinstance(step, dict) and step.get("command"):
                clean_steps.append({
                    "command": str(step["command"]).strip(),
                    "delay": step.get("delay", 0),
                    **(({"on_fail": step["on_fail"]} if step.get("on_fail") else {})),
                })
        data["steps"] = clean_steps
        return data

    def _save_generated(self, workflow: Dict) -> str:
        """Auto-save a generated workflow to the workflows directory."""
        name = workflow.get("name", "generated")
        slug = re.sub(r"[^\w]+", "_", name.lower()).strip("_")[:50]
        filename = f"gen_{slug}.yaml"
        filepath = os.path.join(self.workflows_dir, filename)

        # Avoid overwriting — append a counter
        counter = 1
        while os.path.exists(filepath):
            filename = f"gen_{slug}_{counter}.yaml"
            filepath = os.path.join(self.workflows_dir, filename)
            counter += 1

        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(workflow, f, default_flow_style=False, allow_unicode=True)
        return filepath

    # ── Cross-Domain Context Engine ────────────────────────────────────────

    def _run_with_context(self, name: str, init_vars: Dict[str, Any] | None = None,
                          workflow_data: Dict | None = None):
        """Execute a workflow with full cross-domain dynamic context.

        If workflow_data is provided, runs it in-memory (no file load).
        Otherwise loads from the workflows directory by name.
        """
        if not self._engine:
            return {"success": False, "error": "Engine not connected"}
        if not name and not workflow_data:
            return {"success": False, "error": "Workflow name required"}

        # ── Load workflow ──
        if workflow_data:
            workflow = workflow_data
        else:
            filepath = self._find_workflow_file(name)
            if not filepath:
                return {"success": False, "error": f"Workflow '{name}' not found"}
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    workflow = yaml.safe_load(f)
            except Exception as e:
                return {"success": False, "error": f"Failed to load workflow: {e}"}

        steps = workflow.get("steps", [])
        if not steps:
            return {"success": False, "error": "Workflow has no steps"}

        # ── Build initial context ──
        ctx: Dict[str, str] = {}
        for k, v in (init_vars or {}).items():
            if k != "name":
                ctx[f"init.{k}"] = str(v)

        results = []
        abort = False

        for i, step in enumerate(steps, 1):
            raw_cmd = step.get("command", "")
            delay = step.get("delay", 0)
            on_fail = step.get("on_fail", "continue")  # "continue" | "abort"

            if delay > 0:
                time.sleep(delay)

            # ── Variable injection ──
            cmd = self._inject_context(raw_cmd, ctx)

            # ── Sanitize: collapse stray newlines into semicolons ──
            # YAML double-quoted strings interpret \n as real newlines,
            # which breaks the single-line command parser.
            if "\n" in cmd:
                cmd = re.sub(r"\s*\n\s*", "; ", cmd).strip("; ")

            log.info(f"Workflow '{name}' — Step {i}/{len(steps)}: {cmd}")

            try:
                result = self._engine.execute(cmd)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}

            # ── Capture output into context ──
            self._capture_to_context(ctx, i, cmd, result)

            step_summary = {
                "step": i,
                "command_raw": raw_cmd,
                "command_resolved": cmd if cmd != raw_cmd else None,
                "success": result.get("success", False),
                "result": result.get("result", result.get("error", "")),
            }
            # Include result_data if present (Comet extractions)
            if result.get("result_data"):
                step_summary["result_data"] = result["result_data"]

            results.append(step_summary)

            if not result.get("success", False) and on_fail == "abort":
                log.warning(f"Workflow '{name}' aborting at step {i} (on_fail=abort)")
                abort = True
                break

        successful = sum(1 for r in results if r["success"])
        return {
            "success": not abort and successful == len(results),
            "result": {
                "workflow": name,
                "steps_total": len(steps),
                "steps_executed": len(results),
                "steps_successful": successful,
                "aborted": abort,
                "final_context": {k: v[:200] for k, v in ctx.items()},
                "details": results,
            },
        }

    def _find_workflow_file(self, name: str) -> str | None:
        """Find a workflow YAML file by exact or partial name."""
        filename = f"{name.replace(' ', '_').lower()}.yaml"
        filepath = os.path.join(self.workflows_dir, filename)
        if os.path.exists(filepath):
            return filepath
        for f in os.listdir(self.workflows_dir):
            if name.lower() in f.lower():
                return os.path.join(self.workflows_dir, f)
        return None

    def _inject_context(self, command: str, ctx: Dict[str, str]) -> str:
        """Replace {{var}} placeholders in a command string with context values.

        Newlines in values are collapsed to ' | ' so they don't break
        single-line shell commands.
        """
        def replacer(m):
            key = m.group(1).strip()
            val = ctx.get(key, m.group(0))
            # Collapse newlines to pipe-separated single line
            val = re.sub(r"\s*\n\s*", " | ", val).strip()
            return val
        return re.sub(r"\{\{(.+?)\}\}", replacer, command)

    def _capture_to_context(self, ctx: Dict[str, str], step_num: int,
                            command: str, result: Dict[str, Any]):
        """Extract useful fields from a step result into the workflow context.

        Populates:
          step_N_result, step_N_data, step_N_url
          prev_result, prev_data, prev_url
          <plugin>_result, <plugin>_data, <plugin>_url
        """
        # Determine which value to treat as "result"
        result_text = str(
            result.get("result_data")
            or result.get("result", "")
            or result.get("error", "")
        ).strip()
        result_data = str(result.get("result_data", "")).strip()
        final_url = str(result.get("final_url", "")).strip()

        # Step-indexed
        ctx[f"step_{step_num}_result"] = result_text
        ctx[f"step_{step_num}_data"] = result_data
        if final_url:
            ctx[f"step_{step_num}_url"] = final_url

        # Rolling prev_*
        ctx["prev_result"] = result_text
        ctx["prev_data"] = result_data
        if final_url:
            ctx["prev_url"] = final_url

        # Plugin-keyed (guess plugin from first word of command or from result)
        plugin_name = self._guess_plugin(command)
        if plugin_name:
            ctx[f"{plugin_name}_result"] = result_text
            if result_data:
                ctx[f"{plugin_name}_data"] = result_data
            if final_url:
                ctx[f"{plugin_name}_url"] = final_url

        # Convenience aliases
        if result_data:
            ctx["comet_result"] = result_data  # always update for backward compat

    def _guess_plugin(self, command: str) -> str:
        """Heuristic: guess which plugin a command targets from its prefix."""
        cmd_lower = command.lower().strip()
        prefix_map = {
            "react plan": "comet",
            "comet plan": "comet",
            "web ": "comet",
            "shell ": "shell",
            "run ": "shell",
            "find ": "file_ops",
            "copy ": "file_ops",
            "move ": "file_ops",
            "delete ": "file_ops",
            "organize ": "file_ops",
            "tree ": "file_ops",
            "open ": "app_launcher",
            "launch ": "app_launcher",
            "screenshot": "screen_intel",
            "type ": "keyboard_mouse",
            "click": "keyboard_mouse",
        }
        for prefix, plugin in prefix_map.items():
            if cmd_lower.startswith(prefix):
                return plugin
        return ""

    def list_workflows(self, **kwargs):
        workflows = []
        if os.path.exists(self.workflows_dir):
            for f in os.listdir(self.workflows_dir):
                if f.endswith(".yaml") or f.endswith(".yml"):
                    filepath = os.path.join(self.workflows_dir, f)
                    try:
                        with open(filepath, "r", encoding="utf-8") as fh:
                            data = yaml.safe_load(fh)
                        workflows.append({
                            "name": data.get("name", f),
                            "steps": len(data.get("steps", [])),
                            "created": data.get("created", "unknown"),
                            "file": f,
                        })
                    except Exception:
                        workflows.append({"name": f, "steps": "?", "file": f})

        if not workflows:
            return {"success": True, "result": "(no workflows saved)"}
        return {"success": True, "result": workflows, "count": len(workflows)}

    def delete_workflow(self, name: str = "", **kwargs):
        filename = f"{name.replace(' ', '_').lower()}.yaml"
        filepath = os.path.join(self.workflows_dir, filename)

        if not os.path.exists(filepath):
            for f in os.listdir(self.workflows_dir):
                if name.lower() in f.lower():
                    filepath = os.path.join(self.workflows_dir, f)
                    break
            else:
                return {"success": False, "error": f"Workflow '{name}' not found"}

        try:
            os.remove(filepath)
            return {"success": True, "result": f"Deleted workflow: {name}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
