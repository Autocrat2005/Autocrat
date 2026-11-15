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
