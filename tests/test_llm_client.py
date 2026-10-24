import pytest
import requests

import llm_client


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeHTTP:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for marker, resp in self.responses.items():
            if marker in url:
                return resp
        raise AssertionError(url)


def test_chain_falls_back_gemini_to_groq(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    http = FakeHTTP({
        "generativelanguage": Resp(429, {"error": "quota"}),
        "groq.com": Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]}),
    })
    assert llm_client.generate("sys", "p", llm_client.CUSTOMIZE_CHAIN, want_json=True, http=http) == {"ok": True}
    gemini_url, gemini_kwargs = http.calls[0]
    assert "gemini-2.0-flash:generateContent" in gemini_url and gemini_kwargs["params"] == {"key": "g"}
    assert gemini_kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"
    groq_kwargs = http.calls[1][1]
    assert groq_kwargs["headers"]["Authorization"] == "Bearer q"
    assert groq_kwargs["json"]["response_format"] == {"type": "json_object"}


def test_gemini_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    http = FakeHTTP({"generativelanguage": Resp(200, {"candidates": [{"content": {"parts": [{"text": " Hi "}]}}]})})
    assert llm_client.generate("s", "p", ["gemini"], http=http) == "Hi"


def test_openrouter_first_for_value_props(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, "x")
    http = FakeHTTP({"openrouter.ai": Resp(200, {"choices": [{"message": {"content": "Value."}}]})})
    assert llm_client.generate("s", "p", llm_client.VALUE_PROP_CHAIN, http=http) == "Value."
    assert "openrouter.ai" in http.calls[0][0]


def test_no_keys(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, "")
    with pytest.raises(llm_client.LLMError, match="No API key"):
        llm_client.generate("s", "p", llm_client.CUSTOMIZE_CHAIN)
