import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, keys, upstream
from app.main import app


@pytest.fixture(autouse=True)
def static_key(monkeypatch):
    monkeypatch.setattr(keys, "_state", {"keys": ["k-test"], "cool": {}, "last": 0.0,
                                        "rr": __import__("itertools").count()})
    monkeypatch.setattr(config, "HARVEST_ENABLED", False)
    monkeypatch.setattr(config, "GATEWAY_TOKEN", "")


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake_open(method, path, make_payload, model, stream, headers, estimate):
        path_, payload = make_payload(model)
        seen.append({"path": path_, "payload": payload, "model": model, "stream": stream,
                     "headers": headers})
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", path_)), model

    monkeypatch.setattr(upstream, "open", fake_open)
    return seen


client = TestClient(app)


def test_models_list():
    body = client.get("/v1/models").json()
    ids = [m["id"] for m in body["data"]]
    assert "claude-opus-4-8" in ids and "gpt-4.1" in ids
    assert body["data"][0]["context_window"] > 0


def test_model_lookup_aliases():
    assert client.get("/v1/models/opus").json()["id"] == "claude-opus-4-8"
    assert client.get("/v1/models/nope").status_code == 404


def test_auth_enforced(monkeypatch):
    monkeypatch.setattr(config, "GATEWAY_TOKEN", "s3cret")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/_health").status_code == 200


def test_chat_passthrough_for_openai_model(calls):
    client.post("/v1/chat/completions", json={"model": "gpt", "messages": [
        {"role": "user", "content": "hi"}], "max_tokens": 5})
    assert calls[0]["path"] == "/openai/v1/chat/completions"
    assert calls[0]["payload"]["model"] == "gpt-4.1"


def test_chat_to_claude_translates_and_hoists_system(calls):
    client.post("/v1/chat/completions", json={"model": "sonnet", "max_tokens": 8, "messages": [
        {"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}]})
    payload = calls[0]["payload"]
    assert calls[0]["path"] == "/anthropic/v1/messages"
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_messages_to_gpt_translates(calls):
    client.post("/v1/messages", json={"model": "gpt", "max_tokens": 8,
                                      "messages": [{"role": "user", "content": "hi"}]})
    assert calls[0]["path"] == "/openai/v1/chat/completions"
    assert calls[0]["payload"]["messages"][0]["role"] == "user"


def test_chat_response_is_converted_from_anthropic(monkeypatch, calls):
    async def fake_open(*a, **kw):
        body = {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 1}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", "/")), "claude-haiku-5-5"

    monkeypatch.setattr(upstream, "open", fake_open)
    out = client.post("/v1/chat/completions", json={"model": "haiku", "max_tokens": 8,
                                                    "messages": [{"role": "user", "content": "x"}]}).json()
    assert out["choices"][0]["message"]["content"] == "hello"
    assert out["usage"]["total_tokens"] == 5


def test_streaming_passthrough_returns_sse(calls):
    with client.stream("POST", "/v1/chat/completions", json={
            "model": "gpt", "stream": True, "messages": [{"role": "user", "content": "x"}]}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")


def test_unknown_model_degrades_not_404s(calls):
    r = client.post("/v1/chat/completions", json={"model": "gpt-9-preview",
                                                  "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert calls[0]["payload"]["model"] == "gpt-4.1"


def test_unknown_betas_are_dropped(calls):
    client.post("/v1/messages", json={"model": "sonnet", "max_tokens": 8,
                                      "messages": [{"role": "user", "content": "x"}]},
                headers={"anthropic-beta": "context-1m-2025-08-07,advisor-tool-2026-03-01"})
    assert calls[0]["headers"]["anthropic-beta"] == "context-1m-2025-08-07"


def test_upstream_status_is_preserved(monkeypatch, calls):
    async def bad(*a, **kw):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}
        return httpx.Response(400, json=body, request=httpx.Request("POST", "/")), "claude-sonnet-4-6"

    monkeypatch.setattr(upstream, "open", bad)
    r = client.post("/v1/messages", json={"model": "sonnet", "max_tokens": 8,
                                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400


def test_embeddings_route_forces_embedding_deployment(calls):
    r = client.post("/v1/embeddings", json={"model": "sonnet", "input": "hi"})
    assert r.status_code == 200
    assert calls[0]["path"] == "/openai/v1/embeddings"
    assert calls[0]["payload"]["model"] == "text-embedding-3-large"


def test_embeddings_listed_with_kind():
    models = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert models["text-embedding-3-large"]["kind"] == "embedding"
    assert models["gpt-4.1"]["kind"] == "chat"


def test_chat_with_embedding_model_falls_back_to_chat(calls):
    client.post("/v1/chat/completions", json={"model": "embed",
                                              "messages": [{"role": "user", "content": "x"}]})
    assert calls[0]["payload"]["model"] == "gpt-4.1"


def test_upstream_failure_maps_to_error(monkeypatch):
    async def boom(*a, **kw):
        raise upstream.Failure(429, b'{"error":"slow down"}')

    monkeypatch.setattr(upstream, "open", boom)
    r = client.post("/v1/chat/completions", json={"model": "gpt",
                                                  "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 429
    assert "slow down" in r.json()["error"]["message"]
