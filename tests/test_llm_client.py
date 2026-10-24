import pytest
import requests

import llm_client


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = data if isinstance(data, str) else str(data)

    def json(self):
        return self._data


class FakeHTTP:
    """Routes by URL substring; a list value is consumed in order (one response per call)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _route(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for marker, resp in self.routes.items():
            if marker in url:
                return resp.pop(0) if isinstance(resp, list) else resp
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, **kwargs):
        return self._route("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._route("GET", url, kwargs)


def gemini_ok(text):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def chat_ok(text):
    return Resp(200, {"choices": [{"message": {"content": text}}]})


def keys(monkeypatch, gemini="", groq="", openrouter=""):
    monkeypatch.setenv("GEMINI_API_KEY", gemini)
    monkeypatch.setenv("GROQ_API_KEY", groq)
    monkeypatch.setenv("OPENROUTER_API_KEY", openrouter)
    for p in ("GEMINI", "GROQ", "OPENROUTER"):
        monkeypatch.delenv(f"{p}_MODEL", raising=False)


def test_gemini_key_goes_in_header_never_in_url(monkeypatch):
    keys(monkeypatch, gemini="SECRET-KEY")
    http = FakeHTTP({":generateContent": gemini_ok('{"ok": true}')})
    assert llm_client.generate("s", "p", ["gemini"], want_json=True, http=http) == {"ok": True}
    _, url, kwargs = http.calls[0]
    assert url.endswith("/models/gemini-flash-latest:generateContent")
    assert kwargs["headers"] == {"x-goog-api-key": "SECRET-KEY"} and "params" not in kwargs
    assert "SECRET-KEY" not in url
    assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_retired_gemini_model_is_replaced_automatically(monkeypatch, caplog):
    """The exact failure seen live: GEMINI_MODEL=gemini-2.0-flash -> 404."""
    keys(monkeypatch, gemini="SECRET-KEY")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
    models = {"models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-3-flash-preview-09", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]}
    http = FakeHTTP({
        "gemini-2.0-flash:generateContent": Resp(404, '{"error": {"code": 404, "message": "models/gemini-2.0-flash is not found"}}'),
        "gemini-3-flash:generateContent": gemini_ok("Hello"),
        "/models": Resp(200, models),
    })
    assert llm_client.generate("s", "p", ["gemini"], http=http) == "Hello"
    assert llm_client.current_model("gemini") == "gemini-3-flash"
    assert "set GEMINI_MODEL=gemini-3-flash in .env" in caplog.text
    assert "SECRET-KEY" not in caplog.text
    # the next call goes straight to the discovered model
    http.routes["gemini-3-flash:generateContent"] = gemini_ok("Again")
    assert llm_client.generate("s", "p", ["gemini"], http=http) == "Again"


def test_retired_groq_model_is_replaced(monkeypatch):
    keys(monkeypatch, groq="GROQ-KEY")
    monkeypatch.setenv("GROQ_MODEL", "llama3-70b-8192")
    http = FakeHTTP({
        "/chat/completions": [
            Resp(400, '{"error":{"message":"The model `llama3-70b-8192` has been decommissioned","code":"model_decommissioned"}}'),
            chat_ok('{"ok": 1}'),
        ],
        "/models": Resp(200, {"data": [{"id": "whisper-large-v3"}, {"id": "llama-3.1-8b-instant"},
                                       {"id": "openai/gpt-oss-120b"}, {"id": "meta-llama/llama-guard-4-12b"}]}),
    })
    assert llm_client.generate("s", "p", ["groq"], want_json=True, http=http) == {"ok": 1}
    assert http.calls[-1][2]["json"]["model"] == "openai/gpt-oss-120b"


def test_openrouter_prefers_free_models():
    ids = ["openai/gpt-5", "meta-llama/llama-4-maverick:free", "google/gemma-3-27b-it:free", "mistralai/x"]
    assert llm_client.pick_openai_compatible_model("openrouter", ids) == "meta-llama/llama-4-maverick:free"


def test_pick_gemini_prefers_latest_alias_and_stable_flash():
    def m(*names):
        return [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]

    assert llm_client.pick_gemini_model(m("gemini-2.5-pro", "gemini-flash-latest")) == "gemini-flash-latest"
    assert llm_client.pick_gemini_model(m("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3-flash-image")) == "gemini-2.5-flash"
    assert llm_client.pick_gemini_model(m("gemini-3-flash-preview")) == "gemini-3-flash-preview"  # previews only if nothing stable
    assert llm_client.pick_gemini_model([]) is None


def test_chain_falls_back_gemini_to_groq(monkeypatch):
    keys(monkeypatch, gemini="g", groq="q")
    http = FakeHTTP({":generateContent": Resp(500, "backend error"), "groq.com": chat_ok('{"ok": true}')})
    assert llm_client.generate("sys", "p", llm_client.CUSTOMIZE_CHAIN, want_json=True, http=http) == {"ok": True}
    assert [c[1].split("/")[-1] for c in http.calls[:3]] == [
        "gemini-flash-latest:generateContent", "gemini-flash-lite-latest:generateContent",
        "gemini-2.5-flash-lite:generateContent"]  # overloaded: lighter Gemini models first
    groq_kwargs = http.calls[-1][2]
    assert groq_kwargs["headers"]["Authorization"] == "Bearer q"
    assert groq_kwargs["json"]["response_format"] == {"type": "json_object"}


def test_invalid_key_message_is_clear_and_redacted(monkeypatch):
    keys(monkeypatch, gemini="AQ.SECRET")
    http = FakeHTTP({":generateContent": Resp(400, '{"error": {"message": "API key not valid. Please pass a valid API key. AQ.SECRET", "status": "INVALID_ARGUMENT"}}')})
    with pytest.raises(llm_client.LLMError) as exc:
        llm_client.generate("s", "p", ["gemini"], http=http)
    assert "rejected the API key - check GEMINI_API_KEY" in str(exc.value)
    assert "AQ.SECRET" not in str(exc.value)


def test_rate_limit_message(monkeypatch):
    keys(monkeypatch, gemini="g")
    http = FakeHTTP({":generateContent": Resp(429, "RESOURCE_EXHAUSTED")})
    with pytest.raises(llm_client.LLMError, match="quota"):
        llm_client.generate("s", "p", ["gemini"], http=http)


def test_redaction_of_any_key_in_error_text(monkeypatch):
    keys(monkeypatch, gemini="AQ.Ab8RN6KY")
    assert "AQ.Ab8RN6KY" not in llm_client._redact("url ...?key=AQ.Ab8RN6KY&x=1 and AQ.Ab8RN6KY")


def test_safety_block_reported(monkeypatch):
    keys(monkeypatch, gemini="g")
    http = FakeHTTP({":generateContent": Resp(200, {"promptFeedback": {"blockReason": "SAFETY"}})})
    with pytest.raises(llm_client.LLMError, match="SAFETY"):
        llm_client.generate("s", "p", ["gemini"], http=http)


def test_thought_parts_are_ignored(monkeypatch):
    keys(monkeypatch, gemini="g")
    http = FakeHTTP({":generateContent": Resp(200, {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": "Answer"}]}}]})})
    assert llm_client.generate("s", "p", ["gemini"], http=http) == "Answer"


def test_openrouter_first_for_value_props(monkeypatch):
    keys(monkeypatch, gemini="x", groq="x", openrouter="x")
    http = FakeHTTP({"openrouter.ai": chat_ok("Value.")})
    assert llm_client.generate("s", "p", llm_client.VALUE_PROP_CHAIN, http=http) == "Value."
    assert "openrouter.ai" in http.calls[0][1]


def test_connection_error_falls_through(monkeypatch):
    keys(monkeypatch, gemini="g", groq="q")

    class Down(FakeHTTP):
        def post(self, url, **kwargs):
            if "googleapis" in url:
                raise requests.ConnectionError("DNS failure")
            return super().post(url, **kwargs)

    assert llm_client.generate("s", "p", ["gemini", "groq"], http=Down({"groq.com": chat_ok("hi")})) == "hi"


def test_no_keys(monkeypatch):
    keys(monkeypatch)
    with pytest.raises(llm_client.LLMError, match="No API key"):
        llm_client.generate("s", "p", llm_client.CUSTOMIZE_CHAIN)
