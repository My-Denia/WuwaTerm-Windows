"""``POST /v1/structure-checks`` and the review features of ``/v1/reviews``,
through the real client and its protected HTTP transport."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from wuwaterm_client import structure
from wuwaterm_client.api import ApiClient, review_feature_header
from wuwaterm_client.errors import ERROR_NOT_CONFIGURED, ERROR_UNKNOWN, ClientError

SOURCE = "价格是 100 元。"
TARGET = "The price is 10 yuan."


def make_client(handler, base_url: str | None = "https://test") -> ApiClient:
    return ApiClient(base_url, token_provider=lambda: "example-token",
                     _test_transport=httpx.MockTransport(handler))


def body() -> dict:
    return {"check_version": "structure-check-v1",
            "segments": [{"segment_id": "s0", "source": SOURCE, "target": TARGET},
                         {"segment_id": "s1", "source": "一", "target": ""}]}


def answer() -> dict:
    return {"request_id": "req-l1", "check_version": "structure-check-v1",
            "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
            "segments": [
                {"index": 0, "segment_id": "s0", "omitted_findings": 0,
                 "findings": [
                     {"rule_id": "number_missing", "source_span": {"start": 4, "end": 7, "text": "100"},
                      "target_span": None},
                     {"rule_id": "number_unexpected", "source_span": None,
                      "target_span": {"start": 13, "end": 15, "text": "10"}}],
                 "layout": {"source_paragraphs": 1, "target_paragraphs": 1, "source_lines": 1, "target_lines": 1}},
                {"index": 1, "segment_id": "s1", "omitted_findings": 0,
                 "findings": [{"rule_id": "empty_target", "source_span": None, "target_span": None}],
                 "layout": {"source_paragraphs": 1, "target_paragraphs": 0, "source_lines": 1, "target_lines": 0}},
            ]}


def run(client: ApiClient, coroutine):
    async def scenario():
        try:
            return await coroutine
        finally:
            await client.aclose()
    return asyncio.run(scenario())


def test_a_structure_check_sends_compact_utf8_and_accepts_the_answer_for_these_segments() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer())

    client = make_client(handler)
    result = run(client, client.structure_check(body()))
    assert result == answer()
    request = seen[0]
    assert (request.method, str(request.url)) == ("POST", "https://test/v1/structure-checks")
    assert request.headers["authorization"] == "Bearer example-token"
    assert request.headers["content-type"] == "application/json"
    # The exact bytes the batch limit counted: compact UTF-8, not escaped ASCII.
    assert request.content == structure.encode(body())
    assert "价格".encode() in request.content
    # Not a review: none of the review headers.
    assert "x-wuwaterm-review-features" not in request.headers
    assert "x-wuwaterm-matcher-basis" not in request.headers


@pytest.mark.parametrize("change", [
    lambda v: v["segments"][0].update(segment_id="s9"),
    lambda v: v["segments"].pop(),
    lambda v: v["segments"][0]["findings"][0]["source_span"].update(text="101"),
    lambda v: v.update(check_version="structure-check-v2"),
    lambda v: v["segments"][1]["findings"][0].update(rule_id="spelling"),
    lambda v: v.update(extra=True),
])
def test_an_answer_for_other_segments_is_refused(change) -> None:
    value = copy.deepcopy(answer())
    change(value)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=value)

    client = make_client(handler)
    with pytest.raises(ClientError) as raised:
        run(client, client.structure_check(body()))
    assert raised.value.code == ERROR_UNKNOWN


def test_an_invalid_request_is_never_sent_and_an_unconfigured_client_sends_nothing() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=answer())

    client = make_client(handler)
    bad = {**body(), "segments": body()["segments"] * 33}  # 66 segments, repeated ids
    with pytest.raises(ValueError):
        run(client, client.structure_check(bad))
    unconfigured = make_client(handler, base_url=None)
    with pytest.raises(ClientError) as raised:
        run(unconfigured, unconfigured.structure_check(body()))
    assert raised.value.code == ERROR_NOT_CONFIGURED
    assert calls == []


def test_a_service_error_keeps_its_code_and_request_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {"code": "input_too_long", "message": "x"}, "request_id": "rid"})

    client = make_client(handler)
    with pytest.raises(ClientError) as raised:
        run(client, client.structure_check(body()))
    assert (raised.value.code, raised.value.request_id, raised.value.status_code) == ("input_too_long", "rid", 422)


# -- review features on /v1/reviews --------------------------------------------

REVIEW_SOURCE = "椿和今汐在城楼上。椿看着远方。"
REVIEW_TARGET = "Camellya and Jinhsi stood on the tower."


def ambiguous_report() -> dict:
    at = REVIEW_SOURCE.index("椿", 1)
    return {"request_id": "r", "source_revision": hashlib.sha256(REVIEW_SOURCE.encode()).hexdigest(),
            "target_revision": hashlib.sha256(REVIEW_TARGET.encode()).hexdigest(), "rule_version": "review-v2",
            "matcher_revision": "a" * 64,
            "dictionary": {"schema_version": "t", "source_commit": "t", "term_count": 1, "revision": "b" * 64},
            "coverage": {"evaluated": 0, "not_evaluated": 1, "rules": ["review.term_pair"]},
            "findings": [{"id": f"{at}:{at + 1}:椿", "rule_id": "review.term_pair", "verdict": "not_evaluated",
                          "source_span": {"start": at, "end": at + 1, "text": "椿"}, "target_span": None,
                          "candidates": [{"candidate_id": "c" * 64, "zh": "椿", "en": "Camellya",
                                          "category": "resonator", "sources": []}],
                          "candidates_truncated": False, "reason": "ambiguous_mention"}],
            "truncated": False}


def test_the_feature_header_carries_ambiguous_mention_only_with_reason_v1() -> None:
    assert review_feature_header(()) == "dense-128,reason-v1"
    assert review_feature_header(("ambiguous-mention-v1",)) == "dense-128,reason-v1,ambiguous-mention-v1"
    assert review_feature_header(("ambiguous-mention-v1", "something-else")) == \
        "dense-128,reason-v1,ambiguous-mention-v1"


def test_an_ambiguous_mention_is_accepted_only_when_the_request_asked_for_it() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ambiguous_report())

    client = make_client(handler)
    report = run(client, client.review_pair(REVIEW_SOURCE, REVIEW_TARGET, "en", features=("ambiguous-mention-v1",)))
    assert report["findings"][0]["reason"] == "ambiguous_mention"
    assert seen[0].headers["x-wuwaterm-review-features"] == "dense-128,reason-v1,ambiguous-mention-v1"
    # Without the feature the same answer is not this service's answer to this request.
    client = make_client(handler)
    with pytest.raises(ClientError) as raised:
        run(client, client.review_pair(REVIEW_SOURCE, REVIEW_TARGET, "en"))
    assert raised.value.code == ERROR_UNKNOWN
    assert seen[1].headers["x-wuwaterm-review-features"] == "dense-128,reason-v1"
    assert json.loads(seen[1].content) == {"source": REVIEW_SOURCE, "target": REVIEW_TARGET, "direction": "en",
                                           "review_version": "review-v2"}
