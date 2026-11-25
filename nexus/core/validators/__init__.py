"""
Autocrat — Pluggable code validators.

Every validator runs the same three passes the original core_builder used:
syntax → safety → structure, and returns a ValidationResult whose `message`
keeps the old "[SYNTAX] / [SAFETY] / [STRUCTURE]" prefixes so the LLM
self-heal loop can feed it straight back as a repair hint.

    from nexus.core.validators import get_validator
    result = get_validator("python").validate(source)

Languages: "python" (generated Autocrat plugins). "typescript"/"tsx" is
registered but fails closed until the Phase 3 ts-morph validator lands.
"""

from __future__ import annotations

from typing import Dict

from nexus.core.validators.base import CodeValidator, ValidationResult
from nexus.core.validators.python_plugin import PythonPluginValidator
from nexus.core.validators.typescript import TypeScriptValidator

_REGISTRY: Dict[str, CodeValidator] = {}


def register_validator(validator: CodeValidator, *aliases: str) -> None:
    for name in (validator.language, *aliases):
        _REGISTRY[name.lower()] = validator


def get_validator(language: str) -> CodeValidator:
    try:
        return _REGISTRY[language.lower()]
    except KeyError:
        raise KeyError(f"No validator registered for '{language}'. "
                       f"Available: {sorted(_REGISTRY)}") from None


def available_languages():
    return sorted(_REGISTRY)


register_validator(PythonPluginValidator(), "py")
register_validator(TypeScriptValidator(), "tsx", "ts")

__all__ = [
    "CodeValidator", "ValidationResult", "PythonPluginValidator", "TypeScriptValidator",
    "register_validator", "get_validator", "available_languages",
]
