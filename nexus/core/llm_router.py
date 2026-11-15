"""
Autocrat — LLM router with cost tracking.

One interface over Gemini (google-genai SDK), Anthropic and Ollama. Routes are
chosen per task from config:

    llm:
      routes:   {default: "gemini:<model>", triage: "...", copy: "anthropic:<model>"}
      pricing:  {"<model>": {input: <usd per 1M>, output: <usd per 1M>}}
      monthly_budget_usd: 40

Every call — success or failure — is written to the store's budget_ledger, so
the HUD/Telegram can show burn against the monthly budget. When the month's
spend reaches the budget, paid providers refuse new calls (Ollama still works).

Prices are NOT hard-coded: fill `llm.pricing` from each provider's price page.
Unknown models are recorded at $0 with a one-time warning.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from nexus.core.config import Config
from nexus.core.events import EventBus
from nexus.core.logger import get_logger
from nexus.core.secrets import get_secret

log = get_logger("llm_router")

FREE_PROVIDERS = {"ollama"}


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    data: Optional[Any] = None  # parsed JSON when json_mode=True


class BudgetExceeded(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def complete(self, *, model: str, prompt: str, system: str = "", max_tokens: int = 1024,
                 temperature: float = 0.2, json_mode: bool = False,
                 json_schema: Optional[Dict[str, Any]] = None) -> LLMResult: ...


# ─── Providers ───────────────────────────────────────────────────────────────

class GeminiProvider:
    """Google Gemini via the current `google-genai` SDK (not the deprecated google-generativeai)."""

    name = "gemini"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_secret("GEMINI_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GEMINI_API_KEY is not set")
            from google import genai  # pip install google-genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def complete(self, *, model, prompt, system="", max_tokens=1024, temperature=0.2,
                 json_mode=False, json_schema=None) -> LLMResult:
        from google.genai import types
        cfg: Dict[str, Any] = {"max_output_tokens": max_tokens, "temperature": temperature}
        if system:
            cfg["system_instruction"] = system
        if json_mode or json_schema:
            cfg["response_mime_type"] = "application/json"
        config = types.GenerateContentConfig(**cfg)
        if json_schema:
            try:  # newer SDKs take plain JSON Schema; older ones only the OpenAPI subset
                config = types.GenerateContentConfig(**cfg, response_json_schema=json_schema)
            except Exception:
                config = types.GenerateContentConfig(**cfg, response_schema=json_schema)
        resp = self._get_client().models.generate_content(model=model, contents=prompt, config=config)
        usage = getattr(resp, "usage_metadata", None)
        return LLMResult(
            text=resp.text or "",
            provider=self.name,
            model=model,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_secret("ANTHROPIC_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete(self, *, model, prompt, system="", max_tokens=1024, temperature=0.2,
                 json_mode=False, json_schema=None) -> LLMResult:
        if json_mode or json_schema:
            hint = "Respond with a single JSON object and nothing else."
            if json_schema:
                hint += " It must match this JSON schema: " + json.dumps(json_schema)
            system = f"{system}\n\n{hint}".strip()
        kwargs: Dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self._get_client().messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        return LLMResult(
            text=text, provider=self.name, model=model,
            input_tokens=int(resp.usage.input_tokens or 0),
            output_tokens=int(resp.usage.output_tokens or 0),
        )


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or get_secret("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")

    def complete(self, *, model, prompt, system="", max_tokens=1024, temperature=0.2,
                 json_mode=False, json_schema=None) -> LLMResult:
        import httpx
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        body: Dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_schema:
            body["format"] = json_schema
        elif json_mode:
            body["format"] = "json"
        resp = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return LLMResult(
            text=(data.get("message") or {}).get("content", ""),
            provider=self.name, model=model,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )


# ─── Router ──────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


class LLMRouter:
    def __init__(self, store=None, config: Optional[Config] = None,
                 providers: Optional[Dict[str, LLMProvider]] = None):
        self.config = config or Config()
        self._store = store
        self._providers: Dict[str, LLMProvider] = providers or {}
        self._factories: Dict[str, Callable[[], LLMProvider]] = {
            "gemini": GeminiProvider, "anthropic": AnthropicProvider, "ollama": OllamaProvider,
        }
        self._warned_prices: set = set()
        self._lock = threading.Lock()

    @property
    def store(self):
        if self._store is None:
            from nexus.core.store import get_store
            self._store = get_store()
        return self._store

    def register_provider(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    def provider(self, name: str) -> LLMProvider:
        with self._lock:
            if name not in self._providers:
                factory = self._factories.get(name)
                if not factory:
                    raise ValueError(f"Unknown LLM provider '{name}'")
                self._providers[name] = factory()
            return self._providers[name]

    def route(self, task: str = "default") -> tuple[str, str]:
        routes = self.config.get("llm", "routes", default={}) or {}
        spec = routes.get(task) or routes.get("default") or "ollama:qwen2.5-coder:3b"
        provider, _, model = spec.partition(":")
        return provider.strip(), model.strip()

    def price(self, model: str, input_tokens: int, output_tokens: int, provider: str) -> float:
        if provider in FREE_PROVIDERS:
            return 0.0
        pricing = (self.config.get("llm", "pricing", default={}) or {}).get(model)
        if not pricing:
            if model not in self._warned_prices:
                self._warned_prices.add(model)
                log.warning(f"No price configured for model '{model}' — recording $0. "
                            f"Set llm.pricing.{model} in nexus_config.yaml.")
            return 0.0
        return (input_tokens * float(pricing.get("input", 0)) +
                output_tokens * float(pricing.get("output", 0))) / 1_000_000

    def monthly_budget(self) -> float:
        return float(self.config.get("llm", "monthly_budget_usd", default=40) or 40)

    def budget_status(self) -> Dict[str, Any]:
        spent = self.store.month_spend()
        budget = self.monthly_budget()
        return {**spent, "budget_usd": budget,
                "used_pct": round(100 * spent["cost_usd"] / budget, 2) if budget else 0.0}

    def complete(self, prompt: str, *, task: str = "default", system: str = "",
                 max_tokens: int = 1024, temperature: float = 0.2, json_mode: bool = False,
                 json_schema: Optional[Dict[str, Any]] = None, lead_id: Optional[str] = None,
                 provider: Optional[str] = None, model: Optional[str] = None) -> LLMResult:
        route_provider, route_model = self.route(task)
        provider_name = provider or route_provider
        model_name = model or route_model

        if provider_name not in FREE_PROVIDERS:
            status = self.budget_status()
            if status["budget_usd"] and status["cost_usd"] >= status["budget_usd"]:
                raise BudgetExceeded(
                    f"Monthly LLM budget reached (${status['cost_usd']:.2f} of ${status['budget_usd']:.2f})")

        started = time.time()
        try:
            result = self.provider(provider_name).complete(
                model=model_name, prompt=prompt, system=system, max_tokens=max_tokens,
                temperature=temperature, json_mode=json_mode, json_schema=json_schema)
        except Exception:
            self.store.record_llm_usage(provider=provider_name, model=model_name, task=task,
                                        lead_id=lead_id, ok=False)
            raise
        result.latency_ms = round((time.time() - started) * 1000, 1)
        result.cost_usd = self.price(model_name, result.input_tokens, result.output_tokens, provider_name)
        if json_mode or json_schema:
            result.data = _parse_json(result.text)
        self.store.record_llm_usage(
            provider=provider_name, model=model_name, task=task, input_tokens=result.input_tokens,
            output_tokens=result.output_tokens, cost_usd=result.cost_usd, lead_id=lead_id)
        EventBus().emit("llm.usage", {
            "provider": provider_name, "model": model_name, "task": task, "lead_id": lead_id,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd, "latency_ms": result.latency_ms,
        })
        return result


_router: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def set_router(router: Optional[LLMRouter]) -> None:
    global _router
    _router = router
