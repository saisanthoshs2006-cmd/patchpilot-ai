"""LLM clients with a shared record/replay cache.

Every structured request is keyed by a hash of (model, effort, system, prompt, schema).
- auto:    serve from cache if present, else call the API and record the response.
- replay:  serve from cache only; a miss is an error. Use this on stage: no network, no surprises.
- offline: the AI engine is skipped entirely (handled by the orchestrator).

The cache stores the raw JSON text exactly as the model returned it, so a replayed
review goes through the same parsing and validation as a live one.

Providers: Anthropic (Claude) and Groq (open models such as gpt-oss-120b). Both use
server-enforced JSON-schema output; engines only see the JsonLLM protocol.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar, Protocol

from patchpilot import config


class LLMError(RuntimeError):
    pass


class CacheMiss(LLMError):
    pass


class JsonLLM(Protocol):
    """What engines depend on. Tests pass a fake that implements this."""

    model: str

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], tag: str = "") -> dict[str, Any]: ...


class CachedJsonClient:
    """Cache + bookkeeping. Subclasses implement `_call` and return the raw JSON text."""

    provider = "base"

    def __init__(
        self,
        model: str = config.REASONING_MODEL,
        effort: str = config.REASONING_EFFORT,
        mode: str = config.LLM_MODE,
        cache_dir: Path = config.LLM_CACHE_DIR,
    ):
        self.model, self.effort, self.mode, self.cache_dir = model, effort, mode, cache_dir
        self.stats = {"cache_hits": 0, "api_calls": 0, "input_tokens": 0, "output_tokens": 0, "json_retries": 0}

    def _key(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        blob = json.dumps(
            {"model": self.model, "effort": self.effort, "system": system, "prompt": prompt, "schema": schema},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], tag: str = "") -> dict[str, Any]:
        key = self._key(system, prompt, schema)
        path = self.cache_dir / f"{tag + '-' if tag else ''}{key}.json"
        if path.exists():
            self.stats["cache_hits"] += 1
            return json.loads(json.loads(path.read_text())["response_text"])
        if self.mode == "replay":
            raise CacheMiss(f"No recorded response for {tag or key} (replay mode never calls the API).")

        text = self._call(system, prompt, schema)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.model} returned invalid JSON") from exc
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "tag": tag, "response_text": text}, indent=1))
        return data

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        raise NotImplementedError


class ClaudeClient(CachedJsonClient):
    provider = "anthropic"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None

    def _api(self):
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(timeout=config.LLM_TIMEOUT_S, max_retries=config.LLM_MAX_RETRIES)
        return self._client

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        import anthropic

        try:
            resp = self._api().beta.messages.create(
                model=self.model,
                max_tokens=config.LLM_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines (code review of exploit-looking code can trip
                # one), the API retries on a fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by the Anthropic API; retry shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Anthropic API (network).") from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Anthropic client error: {exc}") from exc
        except TypeError as exc:  # the SDK raises TypeError when no credentials are configured at all
            raise LLMError("No Anthropic credentials found. Set ANTHROPIC_API_KEY, or use replay/offline mode.") from exc

        self.stats["api_calls"] += 1
        self.stats["input_tokens"] += resp.usage.input_tokens
        self.stats["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise LLMError("The model declined to analyze this code.")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Model response was cut off (max_tokens).")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("Model returned no text block.")
        return text


class GroqClient(CachedJsonClient):
    provider = "groq"
    _EFFORT: ClassVar[dict[str, str]] = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None

    def _api(self):
        import groq

        if self._client is None:
            try:
                self._client = groq.Groq(timeout=config.LLM_TIMEOUT_S, max_retries=config.LLM_MAX_RETRIES)
            except groq.GroqError as exc:
                raise LLMError("No Groq credentials found. Set GROQ_API_KEY, or use replay/offline mode.") from exc
        return self._client

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> str:
        import groq

        api = self._api()
        effort = self._EFFORT.get(self.effort, "high")
        # Groq rejects output that fails the schema (code json_validate_failed), most often because
        # reasoning used up the token budget first. Retry, thinking less each time.
        efforts = [effort, *[e for e in ("medium", "low") if e != effort]]
        try:
            for i, eff in enumerate(efforts):
                try:
                    resp = api.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": "result", "strict": True, "schema": schema},
                        },
                        reasoning_effort=eff,
                        max_completion_tokens=config.LLM_MAX_TOKENS,
                    )
                    break
                except groq.BadRequestError as exc:
                    if "json_validate_failed" not in str(exc) or i == len(efforts) - 1:
                        raise
                    self.stats["json_retries"] += 1
        except groq.AuthenticationError as exc:
            raise LLMError("Groq API key missing or invalid. Set GROQ_API_KEY.") from exc
        except groq.RateLimitError as exc:
            raise LLMError("Rate limited by the Groq API; retry shortly.") from exc
        except groq.APIStatusError as exc:
            raise LLMError(f"Groq API error {exc.status_code}: {exc.message}") from exc
        except groq.APIConnectionError as exc:
            raise LLMError("Could not reach the Groq API (network).") from exc

        self.stats["api_calls"] += 1
        if resp.usage:
            self.stats["input_tokens"] += resp.usage.prompt_tokens
            self.stats["output_tokens"] += resp.usage.completion_tokens
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("Model response was cut off (max tokens).")
        if not choice.message.content:
            raise LLMError("Model returned an empty response.")
        return choice.message.content


def make_client(
    mode: str = config.LLM_MODE, model: str = config.REASONING_MODEL, effort: str = config.REASONING_EFFORT,
) -> CachedJsonClient:
    cls = GroqClient if config.LLM_PROVIDER == "groq" else ClaudeClient
    return cls(model=model, mode=mode, effort=effort)
