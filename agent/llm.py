"""Gemini wrapper. One call per guide. Tries each model in GEMINI_MODELS in order, because
free-tier quotas are per model: a 429 on one model falls through to the next."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised with a message that is safe to show to end users."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str


class LLMClient(Protocol):
    async def generate_json(self, system: str, prompt: str) -> LLMResult: ...


def parse_json_loose(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences and stray prose."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found")
        obj = json.loads(t[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


_TRANSIENT = {408, 429, 500, 502, 503, 504}


class GeminiClient:
    def __init__(self, api_key: str, models: list[str], timeout_s: float = 120.0, max_output_tokens: int = 12000):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is missing")
        if not models:
            raise ValueError("GEMINI_MODELS is empty (run `python -m scripts.list_models`)")
        from google import genai  # imported lazily so tests don't need network or a key

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.models = models
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens

    async def _call(self, model: str, system: str, prompt: str, temperature: float) -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=temperature,
            max_output_tokens=self.max_output_tokens,
        )
        resp = await asyncio.wait_for(
            self._client.aio.models.generate_content(model=model, contents=prompt, config=cfg),
            timeout=self.timeout_s,
        )
        text = getattr(resp, "text", None)
        if not text:
            raise LLMError("The model returned an empty response. Please try again.")
        return text

    async def generate_json(self, system: str, prompt: str) -> LLMResult:
        from google.genai import errors

        last_problem = "The AI model is unavailable right now."
        for model in self.models:
            for attempt in range(2):  # second attempt only for malformed JSON
                try:
                    text = await self._call(model, system, prompt, 0.2 if attempt == 0 else 0.0)
                    return LLMResult(parse_json_loose(text), model)
                except ValueError as exc:  # JSON parse failure
                    log.warning("model %s returned unparseable JSON (attempt %d): %s", model, attempt + 1, exc)
                    last_problem = "The AI model returned malformed output. Please try again."
                    continue
                except asyncio.TimeoutError:
                    log.warning("model %s timed out", model)
                    last_problem = "The AI model took too long to respond."
                    break
                except LLMError as exc:
                    last_problem = str(exc)
                    break
                except errors.APIError as exc:
                    code = getattr(exc, "code", None)
                    log.warning("model %s API error %s: %s", model, code, exc)
                    if code in _TRANSIENT or code == 404:
                        last_problem = (
                            "The AI model is rate-limited or busy. Please try again in a minute."
                            if code != 404
                            else f"Model '{model}' was not found for this API key."
                        )
                        break  # next model
                    if code in (401, 403):
                        raise LLMError("The server's Gemini API key was rejected. The site owner needs to fix it.") from exc
                    raise LLMError("The AI model rejected the request.") from exc
        raise LLMError(last_problem)
