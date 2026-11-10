"""ApiClient: capabilities, semantic review and translation context (mock transport)."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from wuwaterm_client import semantic
from wuwaterm_client.api import ApiClient
from wuwaterm_client.errors import ERROR_UNAUTHORIZED, ERROR_UNKNOWN, ClientError

TOKEN = "wtd1.deadbeef.secret"


def _client(handler) -> ApiClient:
    return ApiClient("https://test", token_provider=lambda: TOKEN,
                     _test_transport=httpx.MockTransport(handler))


def _run(client: ApiClient, call):
    async def scenario():
        try:
            return await call(client)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _report(source: str, target: str) -> dict:
    return {
        "request_id": "r", "review_version": "semantic-review-v1",
        "source_revision": hashlib.sha256(source.encode()).hexdigest(),
        "target_revision": hashlib.sha256(target.encode()).hexdigest(),
        "term_basis": {"dictionary_revision": "a" * 64, "matcher_revision": "b" * 64},
        "status": "complete", "status_reason": None, "dropped_unanchored": 0,
        "checked": list(semantic.CATEGORIES), "issues": [], "truncated": False,
        "truncated_reason": None, "omitted_issues": 0,
    }


def test_capabilities_listed_and_unknown_names_kept() -> None:
    body = {"api_version": "v1", "capabilities": ["semantic-review-v1", "later"], "request_id": "r"}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json=body)

    assert _run(_client(handler), lambda c: c.get_capabilities()) == {"semantic-review-v1", "later"}
    assert seen == [("GET", "/v1/capabilities", f"Bearer {TOKEN}")]


def test_capabilities_404_means_an_older_server() -> None:
    client = _client(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    assert _run(client, lambda c: c.get_capabilities()) == frozenset()


def test_capabilities_other_errors_propagate() -> None:
    error = {"error": {"code": "unauthorized", "message": "no"}, "request_id": "r"}
    client = _client(lambda request: httpx.Response(401, json=error))
    with pytest.raises(ClientError) as raised:
        _run(client, lambda c: c.get_capabilities())
    assert raised.value.code == ERROR_UNAUTHORIZED and raised.value.status_code == 401


def test_capabilities_malformed_body_is_unknown() -> None:
    client = _client(lambda request: httpx.Response(200, json={"capabilities": "all"}))
    with pytest.raises(ClientError) as raised:
        _run(client, lambda c: c.get_capabilities())
    assert raised.value.code == ERROR_UNKNOWN


def test_semantic_review_sends_the_encoded_bytes() -> None:
    source, target = "他不会去。", "He will go."
    before = {"source": "上一段。", "target": "Before."}
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(path=request.url.path, method=request.method, body=request.content,
                        type=request.headers["content-type"], auth=request.headers["authorization"])
        return httpx.Response(200, json=_report(source, target))

    report = _run(_client(handler), lambda c: c.semantic_review(source, target, "en", before=before))
    assert report["status"] == "complete"
    assert captured["body"] == semantic.encode_request(source, target, "en", before=before)
    assert (captured["method"], captured["path"], captured["type"], captured["auth"]) == (
        "POST", "/v1/semantic-reviews", "application/json", f"Bearer {TOKEN}")


def test_semantic_review_rejects_a_report_for_other_text() -> None:
    client = _client(lambda request: httpx.Response(200, json=_report("别的", "other")))
    with pytest.raises(ClientError) as raised:
        _run(client, lambda c: c.semantic_review("他不会去。", "He will go.", "en"))
    assert raised.value.code == ERROR_UNKNOWN


def test_translate_context_only_when_given() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"kind": "exact", "text": "x", "direction": "en",
                                         "dictionary_miss": False, "request_id": "r"})

    context = {"before": {"source": "上文", "target": "Before"}}

    async def both(client):
        await client.translate("下文", to="en")
        await client.translate("下文", to="en", context=context)

    _run(_client(handler), both)
    assert bodies == [{"text": "下文", "to": "en"}, {"text": "下文", "to": "en", "context": context}]
