"""
Validator for LLM-generated Python plugins (used by core_builder).

Allowlist, not denylist:
  * every import must be on SAFE_IMPORTS (exact) or under SAFE_PACKAGE_ROOTS
  * import aliases are tracked, so `from subprocess import run as r; r()` or
    `import os as o; o.remove()` resolve to their real names before checking
  * dangerous functions, destructive methods, dunder access and env access
    are rejected wherever they appear

This is a lint gate, not a sandbox: generated code still runs in-process with
the user's privileges, so the gate errs on the side of refusing.
"""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional, Tuple

from nexus.core.validators.base import ValidationResult

# Exact module names a generated plugin may import.
SAFE_IMPORTS = {
    "os", "os.path", "re", "json", "math", "datetime", "time",
    "pathlib", "collections", "itertools", "functools", "typing",
    "hashlib", "base64", "uuid", "random", "string", "textwrap",
    "urllib.parse", "statistics", "dataclasses", "decimal", "fractions",
    "requests", "httpx", "bs4", "PIL", "psutil",
    "nexus.core.plugin", "nexus.core.logger",
}
# Third-party packages whose submodules are fine too (PIL.Image, bs4.element…)
SAFE_PACKAGE_ROOTS = {"PIL", "bs4", "requests", "httpx", "collections", "datetime", "typing"}

# Fully-qualified callables that are never allowed (after alias resolution).
BLOCKED_FUNCTIONS = {
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "open", "breakpoint", "input",
    "os.system", "os.popen", "os.startfile", "os.remove", "os.unlink", "os.rmdir",
    "os.removedirs", "os.rename", "os.renames", "os.replace", "os.kill", "os.killpg",
    "os.chmod", "os.chown", "os.fork", "os.getenv", "os.putenv", "os.unsetenv",
    "os.execl", "os.execle", "os.execlp", "os.execlpe", "os.execv", "os.execve",
    "os.execvp", "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe", "os.posix_spawn",
    "os.posix_spawnp", "os.truncate", "os.link", "os.symlink",
}
BLOCKED_PREFIXES = ("subprocess.", "shutil.", "ctypes.", "winreg.", "_winreg.", "importlib.",
                    "socket.", "multiprocessing.", "pickle.", "marshal.", "builtins.")
# Attribute reads that leak secrets or escape the gate.
BLOCKED_ATTRIBUTES = {"os.environ", "os.environb", "sys.modules"}
# Method names that are destructive on any receiver (Path.unlink, psutil.Process.kill…)
BLOCKED_METHODS = {"unlink", "rmdir", "rmtree", "startfile", "removedirs", "kill",
                   "terminate", "send_signal", "suspend", "chmod", "chown", "hardlink_to",
                   "symlink_to"}
ALLOWED_DUNDERS = {"__init__", "__name__", "__doc__"}
BLOCKED_NAMES = {"__builtins__", "__loader__", "__spec__", "__import__"}

# Defence in depth for things the AST walk might not see (e.g. inside strings fed to eval).
BLOCKED_PATTERNS = [
    r"subprocess\.",
    r"os\.(?:system|popen|exec|spawn|startfile)\b",
    r"shutil\.rmtree\(",
    r"(?:^|[^\w.])eval\s*\(",
    r"(?:^|[^\w.])exec\s*\(",
    r"__import__\s*\(",
    r"ctypes\.",
    r"winreg\.",
    r"socket\.(?:socket|connect|bind|listen)\(",
    r"__subclasses__",
]


def _is_import_allowed(name: str) -> bool:
    return name in SAFE_IMPORTS or name.split(".")[0] in SAFE_PACKAGE_ROOTS


class _SafetyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.aliases: Dict[str, str] = {}
        self.violations: List[str] = []

    # ── imports (record aliases) ─────────────────────────────────
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if not _is_import_allowed(alias.name):
                self.violations.append(f"Import not allowed: {alias.name}")
            local = alias.asname or alias.name.split(".")[0]
            self.aliases[local] = alias.name if alias.asname else alias.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        if node.level:
            self.violations.append(f"Relative import not allowed: {'.' * node.level}{module}")
        elif not _is_import_allowed(module):
            self.violations.append(f"Import not allowed: from {module}")
        for alias in node.names:
            if alias.name == "*":
                self.violations.append(f"Wildcard import not allowed: from {module} import *")
                continue
            full = f"{module}.{alias.name}"
            self.aliases[alias.asname or alias.name] = full
            if self._qualified_blocked(full):
                self.violations.append(f"Blocked function import: {full}")
        self.generic_visit(node)

    # ── name resolution ──────────────────────────────────────────
    def _dotted(self, node: ast.AST) -> Optional[str]:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        base = self.aliases.get(node.id, node.id)
        return ".".join([base] + list(reversed(parts)))

    @staticmethod
    def _qualified_blocked(name: str) -> bool:
        return name in BLOCKED_FUNCTIONS or name.startswith(BLOCKED_PREFIXES) or name in BLOCKED_ATTRIBUTES

    # ── checks ───────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call):
        func = node.func
        qualified = self._dotted(func)
        if qualified and self._qualified_blocked(qualified):
            self.violations.append(f"Blocked function call: {qualified}()")
        elif isinstance(func, ast.Attribute) and func.attr in BLOCKED_METHODS:
            self.violations.append(f"Blocked destructive method: .{func.attr}()")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in ALLOWED_DUNDERS:
            self.violations.append(f"Dunder attribute access not allowed: .{node.attr}")
        qualified = self._dotted(node)
        if qualified and qualified in BLOCKED_ATTRIBUTES:
            self.violations.append(f"Blocked attribute access: {qualified}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in BLOCKED_NAMES:
            self.violations.append(f"Blocked name: {node.id}")
        self.generic_visit(node)


class PythonPluginValidator:
    language = "python"

    def __init__(self, require_plugin_structure: bool = True):
        self.require_plugin_structure = require_plugin_structure

    # Pass 1
    def validate_syntax(self, source: str) -> Tuple[bool, str]:
        try:
            ast.parse(source)
            return True, ""
        except SyntaxError as e:
            return False, f"Syntax error at line {e.lineno}: {e.msg}"

    # Pass 2
    def scan_safety(self, source: str) -> Tuple[bool, List[str]]:
        violations = []
        for pattern in BLOCKED_PATTERNS:
            hits = re.findall(pattern, source, re.MULTILINE)
            if hits:
                violations.append(f"Blocked pattern: {pattern} (found {len(hits)}x)")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False, violations + ["Cannot AST-parse for safety scan (syntax errors)"]
        visitor = _SafetyVisitor()
        visitor.visit(tree)
        seen = set()
        for v in visitor.violations:
            if v not in seen:
                seen.add(v)
                violations.append(v)
        return not violations, violations

    # Pass 3
    def validate_structure(self, source: str) -> Tuple[bool, str]:
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        has_import = has_class = has_setup = has_register = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "nexus.core.plugin" in node.module:
                has_import = has_import or any(a.name == "NexusPlugin" for a in node.names)
            if isinstance(node, ast.ClassDef):
                bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
                if "NexusPlugin" in bases:
                    has_class = True
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "setup":
                            has_setup = True
                            for sub in ast.walk(item):
                                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                                        and sub.func.attr == "register_command":
                                    has_register = True
        issues = []
        if not has_import:
            issues.append("Missing 'from nexus.core.plugin import NexusPlugin'")
        if not has_class:
            issues.append("No class inheriting from NexusPlugin found")
        if not has_setup:
            issues.append("Missing setup() method")
        if not has_register:
            issues.append("No register_command() calls found in setup()")
        return (not issues), "; ".join(issues)

    def validate(self, source: str) -> ValidationResult:
        ok, err = self.validate_syntax(source)
        if not ok:
            return ValidationResult.failed("syntax", [err])
        safe, violations = self.scan_safety(source)
        if not safe:
            return ValidationResult.failed("safety", violations)
        if self.require_plugin_structure:
            ok, err = self.validate_structure(source)
            if not ok:
                return ValidationResult.failed("structure", [err])
        return ValidationResult.passed()
