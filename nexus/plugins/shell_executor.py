"""
NEXUS OS — Shell Executor Plugin
Run any shell/PowerShell command with output capture and timeout.
"""

import subprocess
import os
from typing import Any, Dict
from nexus.core.plugin import NexusPlugin


class ShellExecutorPlugin(NexusPlugin):
    name = "shell_executor"
    description = "Execute shell and PowerShell commands with output capture"
    icon = "🖥️"

    def setup(self):
        self.default_timeout = 30
        self.register_command("run", self.run_command,
                              "Run a shell command", "shell <command>", ["exec", "cmd", "shell"])
        self.register_command("powershell", self.run_powershell,
                              "Run a PowerShell command/script", "powershell <script>", ["ps"])

    def run_command(self, command: str = "", **kwargs):
        if not command:
            return {"success": False, "error": "No command provided"}

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.default_timeout,
                cwd=os.getcwd(),
            )

            output = result.stdout.strip()
            error = result.stderr.strip()

            return {
                "success": result.returncode == 0,
                "result": output if output else "(no output)",
                "stderr": error if error else None,
                "return_code": result.returncode,
                "command": command,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Command timed out after {self.default_timeout}s", "command": command}
        except Exception as e:
            return {"success": False, "error": str(e), "command": command}

    def run_powershell(self, script: str = "", **kwargs):
        if not script:
            return {"success": False, "error": "No PowerShell script provided"}

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=self.default_timeout,
            )

            output = result.stdout.strip()
            error = result.stderr.strip()

            return {
                "success": result.returncode == 0,
                "result": output if output else "(no output)",
                "stderr": error if error else None,
                "return_code": result.returncode,
                "command": f"PowerShell: {script}",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"PowerShell timed out after {self.default_timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}
