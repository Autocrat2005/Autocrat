"""Regression tests for the AST validator bypasses found in the Phase 0 audit."""

import pytest

from nexus.core.validators import available_languages, get_validator

PLUGIN_HEADER = """
from nexus.core.plugin import NexusPlugin
{imports}

class Gen(NexusPlugin):
    name = "gen"
    def setup(self):
        self.register_command("go", self.go)
    def go(self, **kwargs):
{body}
        return {{"success": True}}
"""


def plugin(imports: str, body: str) -> str:
    body = "\n".join("        " + line for line in body.strip().splitlines())
    return PLUGIN_HEADER.format(imports=imports, body=body)


# The five bypasses that passed the old validator, plus close variants.
BYPASSES = {
    "subprocess.check_output": ("import subprocess", "subprocess.check_output(['whoami'])"),
    "aliased from-import": ("from subprocess import run as r", "r(['whoami'])"),
    "importlib ctypes": ("import importlib", "importlib.import_module('ctypes')"),
    "os.startfile": ("import os", "os.startfile('x.exe')"),
    "os.remove": ("import os", "os.remove('C:/important.txt')"),
    # variants
    "module alias": ("import os as o", "o.remove('x')"),
    "from os import remove": ("from os import remove", "remove('x')"),
    "env secrets": ("import os", "key = os.environ['GEMINI_API_KEY']"),
    "getenv": ("import os", "key = os.getenv('GEMINI_API_KEY')"),
    "dunder escape": ("", "x = ().__class__.__mro__[1].__subclasses__()"),
    "builtins": ("", "b = __builtins__"),
    "pathlib unlink": ("from pathlib import Path", "Path('x').unlink()"),
    "psutil kill": ("import psutil", "psutil.Process(1).kill()"),
    "secrets module": ("from nexus.core import secrets", "secrets.get_secret('X')"),
    "relative import": ("from . import thing", "thing()"),
    "sys.modules": ("import sys", "sys.modules['os']"),
}


@pytest.mark.parametrize("label", sorted(BYPASSES))
def test_bypass_is_blocked(label):
    imports, body = BYPASSES[label]
    result = get_validator("python").validate(plugin(imports, body))
    assert not result.ok, f"{label} should be blocked"
    assert result.stage == "safety"
    assert result.message.startswith("[SAFETY]")


def test_clean_plugin_passes():
    source = plugin("import json\nfrom pathlib import Path\nimport httpx",
                    "data = json.dumps({'a': 1})\np = Path('notes').exists()")
    result = get_validator("python").validate(source)
    assert result.ok, result.message


def test_structure_still_enforced():
    result = get_validator("python").validate("x = 1\n")
    assert not result.ok and result.stage == "structure"


def test_syntax_error_reported():
    result = get_validator("python").validate("def broken(:\n")
    assert not result.ok and result.stage == "syntax"


def test_typescript_fails_closed_until_phase3():
    for name in ("typescript", "tsx", "ts"):
        result = get_validator(name).validate("export const x = 1;")
        assert not result.ok and result.stage == "unavailable"
    assert {"python", "typescript"} <= set(available_languages())


def test_core_builder_delegates_to_validator():
    from nexus.plugins.core_builder import CoreBuilderPlugin
    builder = CoreBuilderPlugin.__new__(CoreBuilderPlugin)  # validation needs no setup()
    ok, err = builder._full_validate(plugin("import subprocess", "subprocess.check_output(['x'])"))
    assert not ok and err.startswith("[SAFETY]")
    ok, err = builder._full_validate(plugin("import json", "json.dumps({})"))
    assert ok, err
