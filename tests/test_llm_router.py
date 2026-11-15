import pytest

from nexus.core.config import Config
from nexus.core.llm_router import BudgetExceeded, LLMResult, LLMRouter


class FakeProvider:
    def __init__(self, name, text='{"score": 8}'):
        self.name = name
        self.text = text
        self.calls = 0

    def complete(self, *, model, prompt, system="", max_tokens=1024, temperature=0.2,
                 json_mode=False, json_schema=None):
        self.calls += 1
        return LLMResult(text=self.text, provider=self.name, model=model,
                         input_tokens=1_000_000, output_tokens=100_000)


def make_router(store):
    cfg = Config()
    cfg.set("llm", "routes", {"default": "gemini:flash-x", "triage": "gemini:flash-x", "local": "ollama:q"})
    cfg.set("llm", "pricing", {"flash-x": {"input": 0.10, "output": 0.40}})
    cfg.set("llm", "monthly_budget_usd", 1)
    return LLMRouter(store=store, config=cfg,
                     providers={"gemini": FakeProvider("gemini"), "ollama": FakeProvider("ollama")})


def test_costs_land_in_ledger(store):
    router = make_router(store)
    result = router.complete("score this", task="triage", json_mode=True, lead_id="abc")
    assert result.data == {"score": 8}
    assert result.cost_usd == pytest.approx(0.10 + 0.04)
    spend = store.month_spend()
    assert spend["calls"] == 1 and spend["cost_usd"] == pytest.approx(0.14)


def test_budget_cap_blocks_paid_but_not_local(store):
    router = make_router(store)
    for _ in range(8):  # 8 × $0.14 > $1
        try:
            router.complete("x", task="triage")
        except BudgetExceeded:
            break
    with pytest.raises(BudgetExceeded):
        router.complete("x", task="triage")
    assert router.complete("x", task="local").cost_usd == 0.0


def test_unknown_price_records_zero(store):
    router = make_router(store)
    result = router.complete("x", provider="gemini", model="unpriced-model")
    assert result.cost_usd == 0.0
    assert store.month_spend()["input_tokens"] == 1_000_000


def test_failed_calls_are_recorded(store):
    class Boom(FakeProvider):
        def complete(self, **kwargs):
            raise RuntimeError("provider down")

    router = make_router(store)
    router.register_provider(Boom("gemini"))
    with pytest.raises(RuntimeError):
        router.complete("x")
    assert store.month_spend()["calls"] == 1


def make_paced_router(store, providers):
    cfg = Config()
    cfg.set("llm", "routes", {"default": ["anthropic:haiku", "deepseek:ds"]})
    cfg.set("llm", "pricing", {"haiku": {"input": 0.10, "output": 0.50}, "ds": {"input": 0.30, "output": 1.20}})
    cfg.set("llm", "provider_budgets", {"anthropic": 9.0, "deepseek": 5.0})
    cfg.set("llm", "monthly_budget_usd", 14)
    cfg.set("llm", "budget_window_days", 30)
    cfg.set("llm", "daily_burst", 2.0)
    cfg.set("llm", "max_prompt_tokens", {"haiku": 1000})
    return LLMRouter(store=store, config=cfg, providers={p.name: p for p in providers})


def test_falls_back_when_primary_hits_its_daily_pace(store):
    a, d = FakeProvider("anthropic"), FakeProvider("deepseek")
    router = make_paced_router(store, [a, d])
    # anthropic: cap 9/30*2 = $0.60, fake call $0.15 → 4 calls.
    # deepseek:  cap 5/30*2 = $0.333, fake call $0.42 → 1 call, then both are paced out.
    used = [router.complete("x").provider for _ in range(5)]
    assert used == ["anthropic"] * 4 + ["deepseek"]
    with pytest.raises(BudgetExceeded, match="daily pace"):
        router.complete("x")
    status = router.budget_status()
    assert status["providers"]["anthropic"]["blocked"].startswith("anthropic daily pace")
    assert status["providers"]["deepseek"]["daily_cap_usd"] == pytest.approx(0.3333, abs=1e-3)


def test_oversized_prompt_skips_haiku(store):
    a, d = FakeProvider("anthropic"), FakeProvider("deepseek")
    router = make_paced_router(store, [a, d])
    result = router.complete("y" * 10_000)          # ~2,857 tokens > 1,000 limit for haiku
    assert result.provider == "deepseek" and a.calls == 0


def test_provider_error_falls_through_chain(store):
    class Down(FakeProvider):
        def complete(self, **kwargs):
            raise RuntimeError("503")

    router = make_paced_router(store, [Down("anthropic"), FakeProvider("deepseek")])
    assert router.complete("x").provider == "deepseek"


def test_deepseek_provider_parses_openai_style_response(monkeypatch):
    import httpx
    from nexus.core.llm_router import DeepSeekProvider

    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 5}}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured.update(url=url, body=json, headers=headers)
        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = DeepSeekProvider(api_key="k").complete(model="deepseek-chat", prompt="hi", json_mode=True)
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert (result.input_tokens, result.output_tokens, result.text) == (12, 5, '{"ok": true}')
