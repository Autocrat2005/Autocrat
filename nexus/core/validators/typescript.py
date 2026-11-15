"""TypeScript/TSX validator — placeholder that fails closed until Phase 3.

Phase 3 replaces this with a Node subprocess (ts-morph / TypeScript compiler
API) that checks: no eval / new Function / dangerouslySetInnerHTML, no
external <script>, network calls only to allowlisted domains, and no
password inputs on prospect-branded demos.
"""

from __future__ import annotations

from nexus.core.validators.base import ValidationResult


class TypeScriptValidator:
    language = "typescript"

    def validate(self, source: str) -> ValidationResult:
        return ValidationResult.failed(
            "unavailable",
            ["TypeScript validation is not implemented yet (Phase 3) — refusing to approve code"],
        )
