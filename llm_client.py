"""Generative AI fallback chain: Gemini (primary) -> Groq -> OpenRouter, all returning JSON or text."""

from __future__ import annotations

import json
import logging
import re

import requests

import settings

log = logging.getLogger(__name__)

TIMEOUT = 90
DEFAULT_MODELS = {
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
}
CUSTOMIZE_CHAIN = ["gemini", "groq", "openrouter"]
VALUE_PROP_CHAIN = ["openrouter", "gemini", "groq"]


class LLMError(RuntimeError):
    pass


def _key(provider: str) -> str:
    return settings.env(f"{provider.upper()}_API_KEY")


def _model(provider: str) -> str:
    return settings.env(f"{provider.upper()}_MODEL", DEFAULT_MODELS[provider])


def available_providers(chain: list[str]) -> list[str]:
    return [p for p in chain if _key(p)]


def _call_gemini(system: str, prompt: str, want_json: bool, http) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_model('gemini')}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, **({"responseMimeType": "application/json"} if want_json else {})},
    }
    resp = http.post(url, params={"key": _key("gemini")}, json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    candidates = resp.json().get("candidates") or []
    if not candidates:
        raise LLMError(f"Gemini returned no candidates: {resp.text[:300]}")
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))


def _call_openai_compatible(provider: str, system: str, prompt: str, want_json: bool, http) -> str:
    url = {
        "groq": "https://api.groq.com/openai/v1/chat/completions",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    }[provider]
    body = {
        "model": _model(provider),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.4,
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}
    resp = http.post(url, headers={"Authorization": f"Bearer {_key(provider)}"}, json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    choices = resp.json().get("choices") or []
    if not choices:
        raise LLMError(f"{provider} returned no choices: {resp.text[:300]}")
    return choices[0]["message"]["content"] or ""


def parse_json(text: str) -> dict:
    """Parse model output as JSON, tolerating ```json fences and leading prose."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def generate(system: str, prompt: str, chain: list[str], want_json: bool = False, http=None):
    """Try each configured provider in order; return parsed JSON (dict) or text from the first that works."""
    http = http or requests.Session()
    providers = available_providers(chain)
    if not providers:
        raise LLMError(f"No API key set for any of {chain} (see .env)")
    errors = []
    for provider in providers:
        try:
            if provider == "gemini":
                text = _call_gemini(system, prompt, want_json, http)
            else:
                text = _call_openai_compatible(provider, system, prompt, want_json, http)
            result = parse_json(text) if want_json else text.strip()
            if not result:
                raise LLMError("empty response")
            log.info("LLM response from %s (%s)", provider, _model(provider))
            return result
        except (requests.RequestException, LLMError, ValueError, KeyError) as exc:
            log.warning("%s failed: %s", provider, str(exc)[:200])
            errors.append(f"{provider}: {exc}")
    raise LLMError("All AI providers failed: " + " | ".join(errors))
