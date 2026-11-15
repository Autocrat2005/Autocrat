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
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

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
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    truncated: bool = False  # provider stopped at max_tokens


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

    def __init__(self, api_key: Optional[str] = None, cache_min_chars: Optional[int] = None):
        self.api_key = api_key or get_secret("ANTHROPIC_API_KEY")
        self._client = None
        if cache_min_chars is None:
            cache_min_chars = Config().get("llm", "anthropic_cache_min_chars", default=4000)
        self.cache_min_chars = int(cache_min_chars or 0)

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
        # Current Claude models reject sampling params (temperature/top_p/top_k), so
        # `temperature` is accepted for interface parity but not sent. Haiku 5.5 thinks
        # adaptively by default (thinking bills as output tokens); `effort` is the cost
        # lever, default "low" for this budget (llm.anthropic_effort).
        effort = Config().get("llm", "anthropic_effort", default="low")
        kwargs: Dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if system:
            if self.cache_min_chars and len(system) >= self.cache_min_chars:
                # Reused system prompts (triage rubric, closer playbook) are read
                # from cache at ~10% of the input price on later calls.
                kwargs["system"] = [{"type": "text", "text": system,
                                     "cache_control": {"type": "ephemeral"}}]
            else:
                kwargs["system"] = system
        resp = self._get_client().messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        usage = resp.usage
        return LLMResult(
            text=text, provider=self.name, model=model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            truncated=getattr(resp, "stop_reason", "") == "max_tokens",
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


class DeepSeekProvider:
    """DeepSeek's OpenAI-compatible chat completions API."""

    name = "deepseek"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or get_secret("DEEPSEEK_API_KEY")
        self.base_url = (base_url or get_secret("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")

    def complete(self, *, model, prompt, system="", max_tokens=1024, temperature=0.2,
                 json_mode=False, json_schema=None) -> LLMResult:
        import httpx
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        if json_mode or json_schema:
            hint = "Respond with a single JSON object and nothing else."
            if json_schema:
                hint += " It must match this JSON schema: " + json.dumps(json_schema)
            system = f"{system}\n\n{hint}".strip()
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        body: Dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature, "stream": False}
        if json_mode or json_schema:
            body["response_format"] = {"type": "json_object"}
        resp = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {self.api_key}"})
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        return LLMResult(
            text=(choice.get("message") or {}).get("content", "") or "",
            provider=self.name, model=model,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            truncated=choice.get("finish_reason") == "length",
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


def estimate_tokens(*texts: str) -> int:
    """Cheap, conservative token estimate (~3.5 chars per token)."""
    return int(sum(len(t or "") for t in texts) / 3.5) + 1


class LLMRouter:
    """Routes tasks to providers, falls back down a chain, and paces spend.

    Budget rules (from the `llm` config section):
      monthly_budget_usd   overall cap over the rolling window
      provider_budgets     {provider: usd} prepaid credit per provider
      budget_window_days   rolling window the budgets cover (default 30)
      daily_burst          a provider may spend up to burst x (budget / window) per 24h,
                           so one busy day can't eat the month (default 2.0)
      max_prompt_tokens    {model: n} skip a model when the prompt is larger
                           (Claude Haiku 5.5 bills 5x above 100K-token prompts)
    A route is one "provider:model" or a list tried in order (fallback chain).
    """

    def __init__(self, store=None, config: Optional[Config] = None,
                 providers: Optional[Dict[str, LLMProvider]] = None):
        self.config = config or Config()
        self._store = store
        self._providers: Dict[str, LLMProvider] = providers or {}
        self._factories: Dict[str, Callable[[], LLMProvider]] = {
            "gemini": GeminiProvider, "anthropic": AnthropicProvider, "ollama": OllamaProvider,
            "deepseek": DeepSeekProvider,
        }
        self._warned_prices: set = set()
        self._lock = threading.Lock()

    @property
    def store(self):
        if self._store is None:
            from nexus.core.store import get_store
            self._store = get_store()
        return self._store

    def _cfg(self, key: str, default=None):
        value = self.config.get("llm", key, default=default)
        return default if value is None else value

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

    # ── routing ──────────────────────────────────────────────────

    @staticmethod
    def _split(spec: str) -> Tuple[str, str]:
        provider, _, model = str(spec).partition(":")
        return provider.strip(), model.strip()

    def candidates(self, task: str = "default") -> List[Tuple[str, str]]:
        routes = self._cfg("routes", {}) or {}
        spec = routes.get(task) or routes.get("default") or "ollama:llama3.2:3b"
        specs = spec if isinstance(spec, (list, tuple)) else [spec]
        return [self._split(s) for s in specs]

    def route(self, task: str = "default") -> Tuple[str, str]:
        return self.candidates(task)[0]

    # ── pricing + budgets ────────────────────────────────────────

    def price(self, model: str, input_tokens: int, output_tokens: int, provider: str,
              cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
        if provider in FREE_PROVIDERS:
            return 0.0
        pricing = (self._cfg("pricing", {}) or {}).get(model)
        if not pricing:
            if model not in self._warned_prices:
                self._warned_prices.add(model)
                log.warning(f"No price configured for model '{model}' — recording $0. "
                            f"Set llm.pricing.{model} in nexus_config.yaml.")
            return 0.0
        input_price = float(pricing.get("input", 0))
        return (input_tokens * input_price +
                output_tokens * float(pricing.get("output", 0)) +
                cache_read_tokens * float(pricing.get("cache_read", input_price * 0.1)) +
                cache_write_tokens * float(pricing.get("cache_write", input_price * 1.25))) / 1_000_000

    def window_days(self) -> int:
        return max(1, int(self._cfg("budget_window_days", 30)))

    def monthly_budget(self) -> float:
        return float(self._cfg("monthly_budget_usd", 40) or 0)

    def _window_start(self) -> float:
        return time.time() - self.window_days() * 86400

    def daily_cap(self, budget: float) -> float:
        return budget / self.window_days() * float(self._cfg("daily_burst", 2.0))

    def provider_refusal(self, provider: str) -> Optional[str]:
        """Why this provider may not take another paid call right now (None = OK)."""
        if provider in FREE_PROVIDERS:
            return None
        total = self.monthly_budget()
        if total:
            spent = self.store.spend_since(self._window_start())["cost_usd"]
            if spent >= total:
                return f"overall budget reached (${spent:.2f} of ${total:.2f})"
        budget = (self._cfg("provider_budgets", {}) or {}).get(provider)
        if budget is None:
            return None
        budget = float(budget)
        spent = self.store.spend_since(self._window_start(), provider=provider)["cost_usd"]
        if spent >= budget:
            return f"{provider} budget reached (${spent:.2f} of ${budget:.2f})"
        cap = self.daily_cap(budget)
        today = self.store.spend_since(time.time() - 86400, provider=provider)["cost_usd"]
        if today >= cap:
            return f"{provider} daily pace reached (${today:.3f} of ${cap:.3f} in 24h)"
        return None

    def budget_status(self) -> Dict[str, Any]:
        start = self._window_start()
        spent = self.store.spend_since(start)
        total = self.monthly_budget()
        providers = {}
        for name, budget in (self._cfg("provider_budgets", {}) or {}).items():
            providers[name] = {
                "budget_usd": float(budget),
                "spent_usd": self.store.spend_since(start, provider=name)["cost_usd"],
                "daily_cap_usd": round(self.daily_cap(float(budget)), 4),
                "blocked": self.provider_refusal(name),
            }
        return {**spent, "budget_usd": total, "window_days": self.window_days(),
                "used_pct": round(100 * spent["cost_usd"] / total, 2) if total else 0.0,
                "providers": providers}

    # ── calls ────────────────────────────────────────────────────

    def complete(self, prompt: str, *, task: str = "default", system: str = "",
                 max_tokens: int = 1024, temperature: float = 0.2, json_mode: bool = False,
                 json_schema: Optional[Dict[str, Any]] = None, lead_id: Optional[str] = None,
                 provider: Optional[str] = None, model: Optional[str] = None) -> LLMResult:
        if provider:
            chain = [(provider, model or self.route(task)[1])]
        else:
            chain = self.candidates(task)
            if model:
                chain = [(chain[0][0], model)]

        prompt_tokens = estimate_tokens(prompt, system)
        limits = self._cfg("max_prompt_tokens", {}) or {}
        refusals: List[str] = []
        last_error: Optional[Exception] = None

        for provider_name, model_name in chain:
            limit = limits.get(model_name)
            if limit and prompt_tokens > int(limit):
                refusals.append(f"{model_name}: prompt ~{prompt_tokens} tokens > {limit}")
                continue
            refusal = self.provider_refusal(provider_name)
            if refusal:
                refusals.append(refusal)
                continue
            started = time.time()
            try:
                result = self.provider(provider_name).complete(
                    model=model_name, prompt=prompt, system=system, max_tokens=max_tokens,
                    temperature=temperature, json_mode=json_mode, json_schema=json_schema)
            except Exception as e:
                self.store.record_llm_usage(provider=provider_name, model=model_name, task=task,
                                            lead_id=lead_id, ok=False)
                log.warning(f"LLM {provider_name}:{model_name} failed for task '{task}': {e}")
                last_error = e
                continue
            result.latency_ms = round((time.time() - started) * 1000, 1)
            result.cost_usd = self.price(model_name, result.input_tokens, result.output_tokens, provider_name,
                                         result.cache_read_tokens, result.cache_write_tokens)
            # Record spend BEFORE parsing, so a bad reply never hides its cost.
            self.store.record_llm_usage(
                provider=provider_name, model=model_name, task=task, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, cost_usd=result.cost_usd, lead_id=lead_id)
            if json_mode or json_schema:
                try:
                    result.data = _parse_json(result.text)
                except (ValueError, TypeError) as e:
                    if result.truncated and max_tokens < 16000:
                        log.warning(f"{provider_name}:{model_name} reply cut off at {max_tokens} tokens; retrying with more room")
                        return self.complete(prompt, task=task, system=system, max_tokens=min(16000, max_tokens * 3),
                                             temperature=temperature, json_mode=json_mode, json_schema=json_schema,
                                             lead_id=lead_id, provider=provider_name, model=model_name)
                    log.warning(f"{provider_name}:{model_name} returned invalid JSON for '{task}': {e}")
                    last_error = e
                    continue
            EventBus().emit("llm.usage", {
                "provider": provider_name, "model": model_name, "task": task, "lead_id": lead_id,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd, "latency_ms": result.latency_ms,
            })
            return result

        if last_error is not None:
            raise last_error
        raise BudgetExceeded("No LLM route available: " + "; ".join(refusals))


_router: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def set_router(router: Optional[LLMRouter]) -> None:
    global _router
    _router = router
