"""One wrapper for every LLM call: Gemini first, Groq as backup.

Why a wrapper: free tiers have rate limits and fail now and then. Keeping
retries, rate limiting, caching and JSON parsing here means the rest of the
code just calls `llm.complete_json(prompt)`.

Smoke test your keys with: python -m core.llm
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.text import now_iso


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    cached: bool


class _Provider:
    name = ""

    def __init__(self, model: str, min_interval: float):
        self.model = model
        self.min_interval = min_interval  # seconds between calls, to stay under free-tier limits
        self._last_call = 0.0

    def wait_turn(self) -> None:
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, prompt: str, system: str | None) -> str:
        raise NotImplementedError


class _Gemini(_Provider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, min_interval: float):
        super().__init__(model, min_interval)
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def call(self, prompt: str, system: str | None) -> str:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0,
            ),
        )
        return response.text or ""


class _Groq(_Provider):
    name = "groq"

    def __init__(self, api_key: str, model: str, min_interval: float):
        super().__init__(model, min_interval)
        from groq import Groq

        self._client = Groq(api_key=api_key)

    def call(self, prompt: str, system: str | None) -> str:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
        )
        return response.choices[0].message.content or ""


def parse_json(raw: str) -> dict:
    """Parse model output as a JSON object, tolerating ```json fences."""
    cleaned = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


class LLM:
    """Call `complete_json`. Pass an engine to cache responses in the database."""

    def __init__(self, engine: Engine | None = None, retries: int = 3, providers=None):
        self.engine = engine
        self.retries = retries
        self.backoff = 1.0  # seconds; tests set 0
        self._memory_cache: dict[str, LLMResult] = {}
        self.providers: list[_Provider] = list(providers or [])
        if self.providers:  # injected, e.g. fakes in tests
            return

        if key := os.environ.get("GEMINI_API_KEY"):
            self.providers.append(
                _Gemini(
                    key,
                    os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash",
                    float(os.environ.get("GEMINI_MIN_INTERVAL") or 6),
                )
            )
        if key := os.environ.get("GROQ_API_KEY"):
            self.providers.append(
                _Groq(
                    key,
                    os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile",
                    float(os.environ.get("GROQ_MIN_INTERVAL") or 2),
                )
            )
        if not self.providers:
            raise LLMError("No LLM configured. Set GEMINI_API_KEY and/or GROQ_API_KEY in .env")

    def complete_json(self, prompt: str, system: str | None = None, use_cache: bool = True) -> LLMResult:
        """Return the model's answer as a dict. Tries each provider in order."""
        key = hashlib.sha256(f"{system or ''}\n---\n{prompt}".encode("utf-8")).hexdigest()
        if use_cache and (hit := self._cache_get(key)):
            return hit

        errors = []
        for provider in self.providers:
            for attempt in range(1, self.retries + 1):
                try:
                    provider.wait_turn()
                    data = parse_json(provider.call(prompt, system))
                    result = LLMResult(data, provider.name, provider.model, cached=False)
                    self._cache_put(key, result)
                    return result
                except Exception as exc:  # network error, rate limit, bad JSON
                    errors.append(f"{provider.name} attempt {attempt}: {type(exc).__name__}: {exc}")
                    if attempt < self.retries:
                        time.sleep(self.backoff * 2**attempt)
        raise LLMError("All LLM providers failed:\n" + "\n".join(errors))

    def _cache_get(self, key: str) -> LLMResult | None:
        if key in self._memory_cache:
            return self._memory_cache[key]
        if self.engine is None:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT provider, model, response FROM llm_cache WHERE key = :key"),
                {"key": key},
            ).first()
        if row is None:
            return None
        result = LLMResult(json.loads(row[2]), row[0], row[1], cached=True)
        self._memory_cache[key] = result
        return result

    def _cache_put(self, key: str, result: LLMResult) -> None:
        self._memory_cache[key] = LLMResult(result.data, result.provider, result.model, cached=True)
        if self.engine is None:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_cache (key, provider, model, response, created_at) "
                    "VALUES (:key, :provider, :model, :response, :created_at) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {
                    "key": key,
                    "provider": result.provider,
                    "model": result.model,
                    "response": json.dumps(result.data),
                    "created_at": now_iso(),
                },
            )


def main() -> None:
    load_dotenv()
    llm = LLM()
    print("Providers:", ", ".join(f"{p.name} ({p.model})" for p in llm.providers))
    result = llm.complete_json(
        'Classify this earbuds review. Reply as JSON {"sentiment": "positive|negative|mixed"}.\n'
        "Review: Sound accha hai but battery 2 ghante mein khatam.",
        use_cache=False,
    )
    print(f"Answered by {result.provider} ({result.model}): {result.data}")


if __name__ == "__main__":
    main()
