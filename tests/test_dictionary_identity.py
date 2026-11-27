"""``GET /v1/dictionary`` (``dictionary-identity-v1``) on the client side.

Core ADR 0023, "Authoritative dictionary identity": the served dictionary
publishes its own revision, so a client no longer has to infer which dictionary
is running from the ``(schema_version, source_commit, term_count)`` tuple
``/v1/meta`` and ``/v1/reviews`` carry - a tuple that stays identical when an
entry changes while the revision does not.

What is held here is the wire contract, not the feature: the body a server can
send, the bodies it cannot, and what an absent endpoint means. The workbench's
use of it (provenance, reuse, the fallback for older servers) is exercised on
real widgets in ``test_tm_workbench_ui.py`` and without them in
``test_document_controller.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

import httpx
import pytest

from wuwaterm_client import errors
from wuwaterm_client.api import (
    CAPABILITY_DICTIONARY_IDENTITY,
    ApiClient,
    DictionaryIdentityResult,
)
from wuwaterm_client.errors import ERROR_NOT_CONFIGURED, ERROR_UNAUTHORIZED, ERROR_UNKNOWN, ClientError

REVISION = hashlib.sha256(b"served dictionary").hexdigest()


def _client(handler, **kwargs) -> ApiClient:
    token_provider = kwargs.pop("token_provider", lambda: "wtd1.deadbeef.secret")
    return ApiClient(
        "https://test",
        token_provider=token_provider,
        _test_transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _body(**overrides) -> dict:
    payload = {
        "revision": REVISION,
        "schema_version": "1",
        "source_commit": "a" * 40,
        "source_game_version": "3.7.0",
        "term_count": 10951,
        "request_id": "req-dictionary",
    }
    payload.update(overrides)
    return payload


def _record(answers):
    """A transport that answers every request from ``answers``, recording paths."""
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers(request)

    return handler, seen


# == the body a server sends ================================================


def test_the_dictionary_identity_is_read_field_for_field() -> None:
    handler, seen = _record(lambda request: httpx.Response(200, json=_body()))
    client = _client(handler)

    identity = asyncio.run(client.get_dictionary())

    assert identity == DictionaryIdentityResult(
        revision=REVISION,
        schema_version="1",
        source_commit="a" * 40,
        source_game_version="3.7.0",
        term_count=10951,
        request_id="req-dictionary",
    )
    assert [request.url.path for request in seen] == ["/v1/dictionary"]
    assert seen[0].headers["Authorization"] == "Bearer wtd1.deadbeef.secret"


def test_the_nullable_provenance_fields_may_be_absent_from_the_dictionary() -> None:
    """A dictionary built without a source commit or a game version is still a
    dictionary: the revision is what a memory entry is compared against, and
    the other two fields are ``null`` in the published schema."""
    handler, _seen = _record(
        lambda request: httpx.Response(
            200, json=_body(schema_version=None, source_commit=None, source_game_version=None)
        )
    )

    identity = asyncio.run(_client(handler).get_dictionary())

    assert identity is not None
    assert identity.revision == REVISION
    assert identity.schema_version is None
    assert identity.source_commit is None
    assert identity.source_game_version is None


@pytest.mark.parametrize("value", [" 3.7.0 ", "3.7.0\n", "\t3.7.0"])
def test_the_game_version_is_normalized_like_the_meta_one(value: str) -> None:
    """`/v1/meta` provenance is whitespace-normalized before it is compared with
    a memory entry; the identity's own game version has to leave here in the
    same shape, or the same build would classify a confirmed segment stale
    depending on which endpoint stated it."""
    handler, _seen = _record(lambda request: httpx.Response(200, json=_body(source_game_version=value)))

    identity = asyncio.run(_client(handler).get_dictionary())

    assert identity is not None and identity.source_game_version == "3.7.0"


# == the endpoint an older server does not have =============================


def test_a_404_is_no_identity_rather_than_a_failure() -> None:
    """An older Core answers 404. Reporting that as an error would make the
    workbench treat a working server as broken; reporting it as "no identity"
    leaves the `/v1/meta` provenance path in charge, which is what ADR 0023
    says has to keep working."""
    handler, _seen = _record(lambda request: httpx.Response(404, json={"error": {"code": "not_found"}}))

    assert asyncio.run(_client(handler).get_dictionary()) is None


def test_an_unusable_credential_is_not_read_as_an_absent_endpoint() -> None:
    """401 is a credential the owner can fix, not a server without the
    endpoint. Silently downgrading it would hide the one failure that has a
    next step."""
    handler, _seen = _record(lambda request: httpx.Response(401, json={"error": {"code": "unauthorized"}}))

    with pytest.raises(ClientError) as raised:
        asyncio.run(_client(handler).get_dictionary())

    assert raised.value.code == ERROR_UNAUTHORIZED


# == the bodies it cannot send ==============================================


def _rejecting_cases() -> list:
    """(name, body) pairs of bodies that are not this endpoint's answer."""
    without = lambda key: {k: v for k, v in _body().items() if k != key}  # noqa: E731
    return [
        ("missing-revision", without("revision")),
        ("missing-request-id", without("request_id")),
        ("missing-term-count", without("term_count")),
        ("extra-key", {**_body(), "matcher_revision": "b" * 64}),
        ("uppercase-revision", _body(revision=REVISION.upper())),
        ("short-revision", _body(revision=REVISION[:63])),
        ("hex-prefixed-revision", _body(revision=f"0x{REVISION[:62]}")),
        ("non-hex-revision", _body(revision="z" * 64)),
        ("empty-revision", _body(revision="")),
        ("revision-not-a-string", _body(revision=1234)),
        ("negative-term-count", _body(term_count=-1)),
        ("boolean-term-count", _body(term_count=True)),
        ("fractional-term-count", _body(term_count=10951.5)),
        ("term-count-not-a-number", _body(term_count="10951")),
        ("request-id-not-a-string", _body(request_id=7)),
        ("schema-version-not-a-string", _body(schema_version=1)),
    ]


@pytest.mark.parametrize("name,body", _rejecting_cases(), ids=[case[0] for case in _rejecting_cases()])
def test_a_body_that_is_not_the_contracts_is_refused(name: str, body: dict) -> None:
    """Every one of these reaches a widget or a memory comparison if it is
    accepted: a revision that is not a digest would compare unequal to every
    confirmed entry and mark them all stale (or, worse, equal by accident), and
    an extra key would let an unrelated local service's JSON pass as
    provenance."""
    handler, _seen = _record(lambda request: httpx.Response(200, json=body))

    with pytest.raises(ClientError) as raised:
        asyncio.run(_client(handler).get_dictionary())

    assert raised.value.code == ERROR_UNKNOWN, name


def test_a_body_that_is_not_json_is_refused() -> None:
    handler, _seen = _record(lambda request: httpx.Response(200, text="<html>not this service</html>"))

    with pytest.raises(ClientError) as raised:
        asyncio.run(_client(handler).get_dictionary())

    assert raised.value.code == ERROR_UNKNOWN


# == the client that was never configured ==================================


def test_an_unconfigured_client_sends_nothing_at_all() -> None:
    """The application's zero-request invariant (issues #68 / #80) holds for a
    new endpoint too: no address means no probe, no dictionary request, no
    socket."""
    handler, seen = _record(lambda request: httpx.Response(200, json=_body()))
    client = ApiClient(None, token_provider=lambda: None, _test_transport=httpx.MockTransport(handler))

    with pytest.raises(ClientError) as raised:
        asyncio.run(client.get_dictionary())

    assert raised.value.code == ERROR_NOT_CONFIGURED
    assert seen == []


# == the capability name and the pinned contract ===========================


def test_the_capability_name_is_the_published_one() -> None:
    assert CAPABILITY_DICTIONARY_IDENTITY == "dictionary-identity-v1"


def test_the_wire_shape_is_the_pinned_core_schema(contract_source) -> None:
    """Read the published schema rather than a copy of it: the required keys,
    the revision pattern and the term-count floor this parser enforces are the
    server's, and a change on either side has to show up here."""
    document = json.loads(
        (contract_source("core") / "docs" / "api" / "openapi.json").read_text(encoding="utf-8")
    )
    schema = document["components"]["schemas"]["DictionaryIdentityResponseBody"]
    assert set(schema["required"]) == {
        "revision", "schema_version", "source_commit", "source_game_version", "term_count", "request_id",
    }
    assert schema["additionalProperties"] is False
    # The parser's own expression, compared with the published pattern so the
    # two cannot drift into "almost the same".
    assert re.compile(schema["properties"]["revision"]["pattern"]).pattern == r"^[0-9a-f]{64}$"
    assert schema["properties"]["term_count"]["minimum"] == 0
    assert document["paths"]["/v1/dictionary"]["get"]["responses"]["200"] is not None
    capabilities = document["components"]["schemas"]["CapabilitiesResponseBody"]["properties"]["capabilities"]
    assert CAPABILITY_DICTIONARY_IDENTITY in capabilities["items"]["enum"]
    # Nothing else in the API moved with it: the meta and review shapes still
    # carry the tuple this client falls back to.
    assert "/v1/meta" in document["paths"] and "/v1/reviews" in document["paths"]
    assert "MetaResponseBody" in document["components"]["schemas"]


def test_the_errors_module_has_no_dictionary_specific_code() -> None:
    """An absent endpoint is `None`, and a malformed body is the same
    `unknown` every other unusable body reports. A new error code would need a
    message, a dispatch-table row and a place on screen for a state the client
    already handles by falling back."""
    assert not [name for name in dir(errors) if "DICTIONARY" in name]
