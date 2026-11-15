"""
Autocrat — Plugin discovery.

Finds NexusPlugin subclasses in:
  * nexus/plugins/*.py                       (built-in, public)
  * every directory in AUTOCRAT_PLUGIN_PATHS (external, e.g. the private
    ../autocrat-revenue-plugins/plugins repo)

Modules are inspected with `ast` BEFORE import, so a profile that doesn't
need a plugin never imports it. That matters on the Linux core VM, where
desktop plugins (pyautogui, win32) can't even be imported.

Profiles (AUTOCRAT_PROFILE): core | desk | full. A plugin declares the
profiles it supports via a literal class attribute, e.g.
    profiles = ("core", "desk")
"full" loads everything (local development).
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from nexus.core.logger import get_logger
from nexus.core.secrets import PROJECT_ROOT, env_list, get_secret

log = get_logger("plugin_loader")

BUILTIN_DIR = PROJECT_ROOT / "nexus" / "plugins"
PROFILES = ("core", "desk", "full")
DEFAULT_PROFILES: Tuple[str, ...] = ("desk",)

# Stable load order for built-ins (anything not listed loads after, by name)
BUILTIN_ORDER = [
    "window_manager", "process_controller", "file_ops", "keyboard_mouse",
    "screen_intel", "app_launcher", "clipboard", "system_info", "volume_display",
    "shell_executor", "task_scheduler", "workflow_engine", "smart_actions",
    "power_tools", "intelligence", "comet_web_agent", "core_builder", "pipeline",
]


@dataclass
class PluginSpec:
    plugin_name: str
    class_name: str
    file_path: Path
    profiles: Tuple[str, ...]
    origin: str  # "builtin" | "external"
    module_name: str = ""


def current_profile() -> str:
    profile = (get_secret("AUTOCRAT_PROFILE", "full") or "full").strip().lower()
    if profile not in PROFILES:
        log.warning(f"Unknown AUTOCRAT_PROFILE '{profile}' — using 'full'")
        return "full"
    return profile


def plugin_paths_from_env() -> List[Path]:
    paths = []
    for raw in env_list("AUTOCRAT_PLUGIN_PATHS"):
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        paths.append(path)
    return paths


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def peek_plugin_classes(file_path: Path) -> List[Tuple[str, str, Tuple[str, ...]]]:
    """Return (class_name, plugin_name, profiles) for NexusPlugin subclasses, without importing."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (OSError, SyntaxError) as e:
        log.warning(f"Skipping {file_path}: {e}")
        return []
    found = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
        if "NexusPlugin" not in bases:
            continue
        plugin_name, profiles = None, DEFAULT_PROFILES
        for stmt in node.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                value = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
                targets = [stmt.target.id]
                value = stmt.value
            else:
                continue
            if "name" in targets:
                literal = _literal(value)
                plugin_name = literal if isinstance(literal, str) else plugin_name
            if "profiles" in targets:
                literal = _literal(value)
                if isinstance(literal, (list, tuple, set)):
                    profiles = tuple(str(p) for p in literal)
        found.append((node.name, plugin_name or file_path.stem, profiles))
    return found


def _candidate_files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return []
    files = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
            files.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").exists() and not entry.name.startswith(("_", ".")):
            files.append(entry / "__init__.py")
    return files


def _profile_matches(profile: str, plugin_profiles: Sequence[str]) -> bool:
    return profile == "full" or profile in plugin_profiles


def discover(profile: Optional[str] = None, plugin_paths: Optional[List[Path]] = None,
             enabled_builtins: Optional[Sequence[str]] = None,
             disabled: Optional[Sequence[str]] = None) -> List[PluginSpec]:
    profile = profile or current_profile()
    plugin_paths = plugin_paths_from_env() if plugin_paths is None else plugin_paths
    enabled = set(enabled_builtins or [])
    disabled = set(disabled or [])
    specs: List[PluginSpec] = []
    seen: set = set()

    builtin = []
    for file_path in _candidate_files(BUILTIN_DIR):
        if file_path.parent.name == "generated":
            continue
        for class_name, plugin_name, profiles in peek_plugin_classes(file_path):
            builtin.append(PluginSpec(plugin_name, class_name, file_path, profiles, "builtin",
                                      f"nexus.plugins.{file_path.stem}"))
    order = {name: i for i, name in enumerate(BUILTIN_ORDER)}
    builtin.sort(key=lambda s: (order.get(s.plugin_name, len(order)), s.plugin_name))

    for spec in builtin:
        if enabled and spec.plugin_name not in enabled:
            log.debug(f"Skipping built-in plugin not enabled in config: {spec.plugin_name}")
            continue
        if spec.plugin_name in disabled or not _profile_matches(profile, spec.profiles):
            continue
        specs.append(spec)
        seen.add(spec.plugin_name)

    for directory in plugin_paths:
        if not directory.is_dir():
            log.warning(f"Plugin path not found: {directory}")
            continue
        for file_path in _candidate_files(directory):
            for class_name, plugin_name, profiles in peek_plugin_classes(file_path):
                if plugin_name in disabled or not _profile_matches(profile, profiles):
                    continue
                if plugin_name in seen:
                    log.warning(f"Duplicate plugin name '{plugin_name}' in {file_path} — skipped")
                    continue
                specs.append(PluginSpec(plugin_name, class_name, file_path, profiles, "external"))
                seen.add(plugin_name)
    return specs


def import_plugin_class(spec: PluginSpec):
    """Import the module for a spec and return the plugin class."""
    if spec.origin == "builtin":
        module = importlib.import_module(spec.module_name)
    else:
        digest = hashlib.sha1(str(spec.file_path.parent).encode()).hexdigest()[:8]
        stem = spec.file_path.parent.name if spec.file_path.name == "__init__.py" else spec.file_path.stem
        module_name = f"autocrat_ext_{digest}.{stem}"
        spec.module_name = module_name
        if module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            parent = module_name.split(".")[0]
            if parent not in sys.modules:
                pkg = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(parent, None, is_package=True))
                pkg.__path__ = [str(spec.file_path.parent if spec.file_path.name != "__init__.py"
                                    else spec.file_path.parent.parent)]
                sys.modules[parent] = pkg
            search = [str(spec.file_path.parent)] if spec.file_path.name == "__init__.py" else None
            import_spec = importlib.util.spec_from_file_location(
                module_name, spec.file_path, submodule_search_locations=search)
            module = importlib.util.module_from_spec(import_spec)
            sys.modules[module_name] = module
            try:
                import_spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
    return getattr(module, spec.class_name)
