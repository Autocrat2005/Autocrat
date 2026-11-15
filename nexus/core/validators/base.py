"""Validator interface shared by every language."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol


@dataclass
class ValidationResult:
    ok: bool
    stage: str = "ok"            # syntax | safety | structure | unavailable | ok
    errors: List[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        if self.ok:
            return ""
        return f"[{self.stage.upper()}] {'; '.join(self.errors)}"

    @classmethod
    def passed(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def failed(cls, stage: str, errors: List[str]) -> "ValidationResult":
        return cls(ok=False, stage=stage, errors=list(errors))


class CodeValidator(Protocol):
    language: str

    def validate(self, source: str) -> ValidationResult: ...
