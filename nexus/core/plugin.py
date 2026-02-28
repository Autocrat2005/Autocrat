"""
Autocrat — Plugin Base Class
All automation modules extend this class.
"""

from abc import ABC
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from nexus.core.logger import get_logger
from nexus.core.config import Config


@dataclass
class CommandDef:
    """Definition of a plugin command."""
    name: str
    description: str
    usage: str
    handler: Callable
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


class NexusPlugin(ABC):
    """Base class for all Autocrat plugins."""

    name: str = "unnamed"
    description: str = "No description"
    version: str = "1.0.0"
    icon: str = "⚡"

    def __init__(self):
        self.log = get_logger(f"plugin.{self.name}")
        self.config = Config()
        self._commands: Dict[str, CommandDef] = {}
        self._enabled = True
        self.setup()

    def setup(self):
        """Override to register commands and do initialization."""
        pass

    def register_command(
        self,
        name: str,
        handler: Callable,
        description: str = "",
        usage: str = "",
        aliases: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ):
        """Register a command with this plugin."""
        cmd = CommandDef(
            name=name,
            description=description,
            usage=usage or name,
            handler=handler,
            aliases=aliases or [],
            keywords=keywords or [],
        )
        self._commands[name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def execute(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a command by name."""
        cmd = self._commands.get(action)
        if not cmd:
            return {
                "success": False,
                "error": f"Unknown command '{action}' in plugin '{self.name}'",
                "available": list(set(c.name for c in self._commands.values())),
            }
        try:
            if self._is_safe_mode_active(cmd.name):
                return {
                    "success": True,
                    "safe_mode": True,
                    "result": f"[SAFE MODE ACTIVE] Would have executed: {self.name}.{cmd.name}({args or {}})",
                    "action": f"{self.name}.{cmd.name}",
                    "args": args or {},
                }

            self.log.info(f"Executing: {action} {args}")
            result = cmd.handler(**args) if args else cmd.handler()
            if not isinstance(result, dict):
                result = {"success": True, "result": result}
            elif "success" not in result:
                result["success"] = True
            return result
        except Exception as e:
            self.log.error(f"Command '{action}' failed: {e}")
            return {"success": False, "error": str(e)}

    def _is_safe_mode_active(self, canonical_action: str) -> bool:
        """Global kill switch for side-effectful execution."""
        safe_mode = bool(self.config.get("system", "safe_mode", default=False))
        if not safe_mode:
            return False

        # Optional allowlist for explicitly permitted actions in safe mode.
        allow = self.config.get("system", "safe_mode_allow_actions", default=[]) or []
        allow_set = {str(a).strip().lower() for a in allow if str(a).strip()}
        action_name = f"{self.name}.{canonical_action}".lower()
        return action_name not in allow_set

    def get_commands(self) -> List[Dict[str, str]]:
        """Get list of unique commands with their info."""
        seen = set()
        commands = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                commands.append({
                    "name": cmd.name,
                    "description": cmd.description,
                    "usage": cmd.usage,
                    "aliases": cmd.aliases,
                })
        return commands

    def get_help(self) -> str:
        """Generate help text for this plugin."""
        lines = [
            f"{self.icon}  {self.name} v{self.version}",
            f"   {self.description}",
            "",
            "   Commands:",
        ]
        for cmd_info in self.get_commands():
            aliases = f" (aliases: {', '.join(cmd_info['aliases'])})" if cmd_info['aliases'] else ""
            lines.append(f"   • {cmd_info['usage']}{aliases}")
            if cmd_info['description']:
                lines.append(f"     {cmd_info['description']}")
        return "\n".join(lines)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        status = "enabled" if value else "disabled"
        self.log.info(f"Plugin {self.name} {status}")
