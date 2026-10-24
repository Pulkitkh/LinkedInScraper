"""Generative AI fallback chain: Gemini (primary) -> Groq -> OpenRouter, all returning JSON or text.

Model names get retired often. If the configured model no longer exists, the provider's model list is
fetched once and the best current model is used instead (and a warning says which one to put in .env).
API keys are sent in headers only, so they never appear in URLs, logs or error messages.
"""

from __future__ import annotations

import json
import logging
import re

import requests

import settings

log = logging.getLogger(__name__)

TIMEOUT = 90
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_COMPATIBLE = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
DEFAULT_MODELS = {
    "gemini": "gemini-flash-latest",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
}
CUSTOMIZE_CHAIN = ["gemini", "groq", "openrouter"]
VALUE_PROP_CHAIN = ["openrouter", "gemini", "groq"]

_resolved: dict[str, str] = {}  # provider -> model picked by discovery for this run


class LLMError(RuntimeError):
    pass


class ModelNotFound(LLMError):
    pass


def _key(provider: str) -> str:
    return settings.env(f"{provider.upper()}_API_KEY")


def current_model(provider: str) -> str:
    return _model(provider)


def _model(provider: str) -> str:
    return _resolved.get(provider) or settings.env(f"{provider.upper()}_MODEL", DEFAULT_MODELS[provider])


def available_providers(chain: list[str]) -> list[str]:
    return [p for p in chain if _key(p)]


def _redact(text: str) -> str:
    """Strip anything that looks like an API key from error text."""
    for provider in OPENAI_COMPATIBLE.keys() | {"gemini"}:
        key = _key(provider)
        if key:
            text = text.replace(key, "***")
    return re.sub(r"([?&]key=)[^&\s]+", r"\1***", text)


def _raise_for_status(resp, provider: str) -> None:
    if resp.status_code < 400:
        return
    body = (resp.text or "")[:400]
    lowered = body.lower()
    model_gone = resp.status_code == 404 or (
        resp.status_code == 400 and "model" in lowered
        and any(w in lowered for w in ("not found", "does not exist", "decommissioned", "not supported",
                                      "deprecated", "invalid model", "no endpoints")))
    if resp.status_code in (400, 401, 403) and any(
            w in lowered for w in ("api key not valid", "api_key_invalid", "invalid api key", "invalid_api_key",
                                   "unauthorized", "no auth credentials", "permission_denied")):
        raise LLMError(f"{provider} rejected the API key - check {provider.upper()}_API_KEY in .env")
    if resp.status_code == 429:
        raise LLMError(f"{provider} rate limit / quota reached - try again later or use another provider")
    message = f"{provider} HTTP {resp.status_code}: {_redact(body)}"
    raise ModelNotFound(message) if model_gone else LLMError(message)


# ------------------------------------------------------------------ calls

def _call_gemini(system: str, prompt: str, want_json: bool, http) -> str:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, **({"responseMimeType": "application/json"} if want_json else {})},
    }
    resp = http.post(f"{GEMINI_BASE}/models/{_model('gemini')}:generateContent",
                     headers={"x-goog-api-key": _key("gemini")}, json=body, timeout=TIMEOUT)
    _raise_for_status(resp, "gemini")
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"Gemini returned no answer ({reason})")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _call_openai_compatible(provider: str, system: str, prompt: str, want_json: bool, http) -> str:
    body = {
        "model": _model(provider),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.4,
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}
    resp = http.post(f"{OPENAI_COMPATIBLE[provider]}/chat/completions",
                     headers={"Authorization": f"Bearer {_key(provider)}"}, json=body, timeout=TIMEOUT)
    _raise_for_status(resp, provider)
    choices = resp.json().get("choices") or []
    if not choices:
        raise LLMError(f"{provider} returned no choices")
    return choices[0]["message"]["content"] or ""


def _call(provider: str, system: str, prompt: str, want_json: bool, http) -> str:
    if provider == "gemini":
        return _call_gemini(system, prompt, want_json, http)
    return _call_openai_compatible(provider, system, prompt, want_json, http)


# ------------------------------------------------------------------ model discovery

def _version(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name)[:3]) or (0,)


_NOT_TEXT = re.compile(r"embed|image|tts|audio|live|vision|whisper|guard|veo|imagen|aqa|learnlm|robotics|"
                       r"computer-use|native", re.I)
_UNSTABLE = re.compile(r"preview|-exp\b|thinking-exp", re.I)


def _usable(names: list[str]) -> list[str]:
    """Text models, preferring stable ones; previews only if nothing stable exists."""
    text_models = [n for n in names if not _NOT_TEXT.search(n)]
    return [n for n in text_models if not _UNSTABLE.search(n)] or text_models


def pick_gemini_model(models: list[dict]) -> str | None:
    names = [m["name"].split("/", 1)[-1] for m in models
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    if "gemini-flash-latest" in names:
        return "gemini-flash-latest"
    usable = _usable([n for n in names if n.startswith("gemini")])
    for tier in (r"flash(?!-lite)", r"flash-lite", r"pro"):
        tiered = [n for n in usable if re.search(tier, n)]
        if tiered:
            return max(tiered, key=lambda n: (_version(n), -len(n)))
    return usable[0] if usable else None


def pick_openai_compatible_model(provider: str, ids: list[str]) -> str | None:
    usable = _usable(ids)
    if provider == "openrouter":
        usable = [i for i in usable if i.endswith(":free")] or usable
    preferences = [r"llama-3\.3-70b", r"llama.*70b", r"gpt-oss-120b", r"llama-4", r"gpt-oss", r"qwen",
                   r"deepseek", r"gemma", r"mistral", r"llama"]
    for pattern in preferences:
        matches = sorted((i for i in usable if re.search(pattern, i, re.I)), key=_version, reverse=True)
        if matches:
            return matches[0]
    return usable[0] if usable else None


def discover_model(provider: str, http) -> str | None:
    """Ask the provider which models exist and pick the best current one."""
    try:
        if provider == "gemini":
            resp = http.get(f"{GEMINI_BASE}/models", params={"pageSize": 1000},
                            headers={"x-goog-api-key": _key("gemini")}, timeout=30)
            _raise_for_status(resp, provider)
            return pick_gemini_model(resp.json().get("models", []))
        resp = http.get(f"{OPENAI_COMPATIBLE[provider]}/models",
                        headers={"Authorization": f"Bearer {_key(provider)}"}, timeout=30)
        _raise_for_status(resp, provider)
        return pick_openai_compatible_model(provider, [m["id"] for m in resp.json().get("data", [])])
    except (requests.RequestException, LLMError, ValueError, KeyError) as exc:
        log.warning("Could not list %s models: %s", provider, _redact(str(exc))[:200])
        return None


# ------------------------------------------------------------------ public API

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


def _attempt(provider: str, system: str, prompt: str, want_json: bool, http):
    try:
        text = _call(provider, system, prompt, want_json, http)
    except ModelNotFound:
        old = _model(provider)
        new = discover_model(provider, http)
        if not new or new == old:
            raise
        log.warning("%s model %r is no longer available; using %r (set %s_MODEL=%s in .env)",
                    provider, old, new, provider.upper(), new)
        _resolved[provider] = new
        text = _call(provider, system, prompt, want_json, http)
    result = parse_json(text) if want_json else text.strip()
    if not result:
        raise LLMError("empty response")
    return result


def generate(system: str, prompt: str, chain: list[str], want_json: bool = False, http=None):
    """Try each configured provider in order; return parsed JSON (dict) or text from the first that works."""
    http = http or requests.Session()
    providers = available_providers(chain)
    if not providers:
        raise LLMError(f"No API key set for any of {chain} (see .env)")
    errors = []
    for provider in providers:
        try:
            result = _attempt(provider, system, prompt, want_json, http)
            log.info("LLM response from %s (%s)", provider, _model(provider))
            return result
        except (requests.RequestException, LLMError, ValueError, KeyError) as exc:
            message = _redact(str(exc))[:300]
            log.warning("%s failed: %s", provider, message)
            errors.append(f"{provider}: {message}")
    raise LLMError("All AI providers failed: " + " | ".join(errors))
