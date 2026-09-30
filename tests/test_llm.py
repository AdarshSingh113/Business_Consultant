import pytest

from core.llm import LLM, LLMError, parse_json
from tests.fakes import FakeProvider as Fake


def make(providers, engine=None):
    llm = LLM(engine=engine, retries=2, providers=providers)
    llm.backoff = 0
    return llm


def test_parse_json_strips_code_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("[1, 2]")


def test_falls_back_to_second_provider():
    gemini = Fake("gemini", [RuntimeError("429 rate limited"), "not json"])
    groq = Fake("groq", ['{"sentiment": "negative"}'])
    result = make([gemini, groq]).complete_json("review")
    assert (result.provider, result.data) == ("groq", {"sentiment": "negative"})
    assert gemini.calls == 2


def test_raises_when_every_provider_fails():
    with pytest.raises(LLMError, match="All LLM providers failed"):
        make([Fake("gemini", [RuntimeError("down")] * 2)]).complete_json("review")


def test_cache_survives_new_instance(engine):
    first = Fake("gemini", ['{"ok": true}'])
    assert make([first], engine).complete_json("same prompt").cached is False

    second = Fake("gemini", [])
    result = make([second], engine).complete_json("same prompt")
    assert (result.cached, result.data, second.calls) == (True, {"ok": True}, 0)
