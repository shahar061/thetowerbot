"""Provider transport regressions; every HTTP exchange is in memory."""

import asyncio
import base64
from dataclasses import replace
import json
import time

import httpx2
import pytest

from recovery_policy import (
    RecoveryCandidate, RecoveryContext, RecoveryImage, RecoveryRequest,
    RecoveryScope, RecoverySettings, VerifiedControl,
)


KEY = "test-secret-never-log"
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lN8AAAAASUVORK5CYII=")
MODEL = "openai/gpt-5.4-nano"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
METADATA = f"https://openrouter.ai/api/v1/models/{MODEL}/endpoints"


def request(*, image: bool = True) -> RecoveryRequest:
    now = time.monotonic()
    scope = RecoveryScope(worker="worker", account_id="account-secret-314", lease_id="lease",
                          attempt_id="attempt", attempt_generation="generation", boot_id="boot",
                          worker_generation=1, strategy_revision=2, device_command_generation=3,
                          pending_transaction_generation=4)
    candidate = RecoveryCandidate(candidate_id="close-1", action_id="close_overlay",
                                  screen="home", control_generation=1, target="settings overlay",
                                  expected_postcondition="overlay_absent",
                                  control=VerifiedControl(x=120, y=330, width=40, height=30,
                                                          frame_width=1080, frame_height=2400))
    context = RecoveryContext(scope=scope, screen="home", screen_generation=1,
                              observation_generation=2, observed_at_monotonic=now - 1,
                              candidates=(candidate,))
    return RecoveryRequest(request_id="req-1", incident_id="incident", fingerprint="fp",
                           context=context, created_at_monotonic=now,
                           deadline_at_monotonic=now + 15, objective="Return to home",
                           image=RecoveryImage(media_type="image/png", data=TINY_PNG,
                                               width=1, height=1) if image else None)


def metadata(*, modalities: tuple[str, ...] = ("text", "image"),
             parameters: tuple[str, ...] = ("max_tokens", "response_format"),
             pricing: dict[str, str] | None = None) -> dict:
    return {"data": {"id": MODEL, "architecture": {"input_modalities": list(modalities),
            "output_modalities": ["text"]}, "endpoints": [{
                "model_id": MODEL, "tag": "openai", "max_prompt_tokens": 400000,
                "max_completion_tokens": 4096, "supported_parameters": list(parameters),
                "pricing": pricing if pricing is not None else {
                    "prompt": "0.0000002", "completion": "0.0000002",
                    "image": "0", "request": "0"},
                "architecture": {"input_modalities": list(modalities),
                                 "output_modalities": ["text"]},
            }]}}


def completion(*, content: str | None = None, model: str = MODEL,
               finish: str = "stop", usage: dict | None = None) -> dict:
    return {"model": model, "choices": [{"finish_reason": finish, "message": {
        "content": content if content is not None else json.dumps({
            "action_id": "close_overlay", "candidate_id": "close-1",
            "expected_postcondition": "overlay_absent", "explanation": "Close the overlay",
        })}}], "usage": usage if usage is not None else {
            "prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001}}


def exchange(meta: dict | None = None, reply: dict | None = None,
             status: int = 200, *, post_error: Exception | None = None):
    captured: list[httpx2.Request] = []

    async def handle(item: httpx2.Request) -> httpx2.Response:
        captured.append(item)
        if item.method == "GET":
            return httpx2.Response(200, json=meta if meta is not None else metadata())
        if post_error is not None:
            raise post_error
        return httpx2.Response(status, json=reply if reply is not None else completion())

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle)), captured


def run(coro):
    return asyncio.run(coro)


async def _preflight_propose(req, settings, *, api_key: str, client):
    from openrouter_recovery import get_model_capability, propose_recovery

    quote = await get_model_capability(req, settings, api_key=api_key, client=client,
                                       deadline_seconds=settings.deadline_seconds)
    return await propose_recovery(req, settings, api_key=api_key, client=client,
                                  reserved_capability=quote)


def test_valid_request_has_fixed_payload_and_redacted_projection() -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange()
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error is None
    assert result.proposal.candidate_id == "close-1"
    assert result.actual_model == MODEL
    assert result.usage.prompt_tokens == 100
    assert result.cost_microusd == 1000
    assert [(r.method, str(r.url)) for r in captured] == [("GET", METADATA), ("POST", ENDPOINT)]
    body = json.loads(captured[1].content)
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["max_tokens"] == 600
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["only"] == ["openai"]
    assert body["provider"]["max_price"] == {
        "prompt": 0.2, "completion": 0.2, "image": 0, "request": 0,
    }
    assert body["response_format"]["json_schema"]["strict"] is True
    schema = body["response_format"]["json_schema"]["schema"]
    assert "candidate_id" in schema["required"]
    assert schema["additionalProperties"] is False
    image_url = body["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    wire = captured[1].content.decode()
    for secret in (KEY, "account-secret-314", "lease", "generation", "120", "330", "/Users/"):
        assert secret not in wire
    context = json.loads(body["messages"][1]["content"][0]["text"])
    assert "account_state" not in context
    assert context["candidates"] == [{"candidate_id": "close-1", "action_id": "close_overlay",
                                      "target": "settings overlay",
                                      "expected_postcondition": "overlay_absent"}]


@pytest.mark.parametrize(("reply", "error"), [
    ({"model": MODEL, "choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}, "invalid_response"),
    ({"model": MODEL, "choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}, "invalid_response"),
    (completion(finish="length"), "invalid_response"),
    (completion(model="other/model"), "model_mismatch"),
    (completion(model=""), "model_mismatch"),
    (completion(content='{"action_id":"close_overlay","candidate_id":"invented","expected_postcondition":"overlay_absent","explanation":"Tap"}'), "invalid_proposal"),
    (completion(content='{"action_id":"shell","candidate_id":null,"expected_postcondition":"paused","explanation":"Run code"}'), "invalid_proposal"),
])
def test_untrusted_output_never_becomes_proposal(reply: dict, error: str) -> None:
    from openrouter_recovery import propose_recovery

    client, _ = exchange(reply=reply)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == error
    assert result.proposal is None


def test_missing_usage_stays_unknown() -> None:
    from openrouter_recovery import propose_recovery

    reply = completion()
    del reply["usage"]
    client, _ = exchange(reply=reply)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error is None
    assert result.usage is None
    assert result.cost_microusd is None


def test_actual_cost_over_quote_is_reported_without_proposal() -> None:
    from openrouter_recovery import propose_recovery

    reply = completion(usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.02})
    client, _ = exchange(reply=reply)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "budget_exhausted"
    assert result.proposal is None
    assert result.cost_microusd == 20_000


def test_wrong_model_retains_reported_usage_for_settlement() -> None:
    from openrouter_recovery import propose_recovery

    client, _ = exchange(reply=completion(model="other/model"))
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "model_mismatch"
    assert result.actual_model == "other/model"
    assert result.usage.prompt_tokens == 100
    assert result.cost_microusd == 1000


def test_provider_model_field_cannot_echo_secret_into_reply() -> None:
    from openrouter_recovery import propose_recovery

    client, _ = exchange(reply=completion(model=KEY))
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "model_mismatch"
    assert result.actual_model is None
    assert KEY not in repr(result)


@pytest.mark.parametrize(("status", "error"), [(401, "authentication"), (402, "budget_exhausted"),
                                                  (429, "rate_limited"), (500, "http")])
def test_http_errors_are_classified_without_body(status: int, error: str) -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange(status=status, reply={"secret": KEY})
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == error
    assert result.http_status == status
    assert KEY not in repr(result)
    assert len(captured) == 2


def test_connection_failure_has_no_retry_or_secret() -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange(post_error=httpx2.ConnectError(KEY))
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "transport"
    assert len(captured) == 2
    assert KEY not in repr(result)


def test_provider_read_timeout_is_classified_as_timeout() -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange(post_error=httpx2.ReadTimeout(KEY))
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "timeout"
    assert len(captured) == 2


def test_sensitive_text_is_redacted_before_post() -> None:
    from openrouter_recovery import propose_recovery

    original = request()
    altered = original.model_copy(update={
        "objective": f"Return to /Users/operator/private and {original.context.scope.account_id} API_KEY={KEY}",
    })
    client, captured = exchange()
    result = run(_preflight_propose(altered, RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error is None
    payload = json.loads(captured[1].content)
    objective = json.loads(payload["messages"][1]["content"][0]["text"])["objective"]
    assert objective.count("[redacted]") >= 2
    assert "/Users/operator" not in objective
    assert "account-secret-314" not in objective
    assert KEY not in captured[1].content.decode()


def test_missing_image_blocks_post() -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange()
    result = run(_preflight_propose(request(image=False), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error is not None
    assert captured == []


def test_total_deadline_includes_slow_completion() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    captured: list[str] = []

    async def slow(item: httpx2.Request) -> httpx2.Response:
        captured.append(item.method)
        if item.method == "GET":
            return httpx2.Response(200, json=metadata())
        await asyncio.sleep(0.05)
        return httpx2.Response(200, json=completion())

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(slow))
    req = request()
    settings = RecoverySettings(mode="shadow", deadline_seconds=0.01)

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=quote)

    result = run(check())
    assert result.error == "timeout"
    assert captured == ["GET", "POST"]


@pytest.mark.parametrize("meta", [
    metadata(modalities=("text",)),
    metadata(parameters=("max_tokens",)),
])
def test_unproven_capability_blocks_post(meta: dict) -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange(meta=meta)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_capability_quote_is_cached_and_bounded() -> None:
    from openrouter_recovery import get_model_capability

    client, captured = exchange()

    async def check():
        first = await get_model_capability(request(), RecoverySettings(mode="shadow"),
                                           api_key=KEY, client=client, deadline_seconds=1)
        second = await get_model_capability(request(), RecoverySettings(mode="shadow"),
                                            api_key=KEY, client=client, deadline_seconds=1)
        return first, second

    first, second = run(check())
    assert first == second
    assert first.worst_case_microusd > 0
    assert first.worst_case_microusd <= 50_000
    assert first.checked_at_monotonic <= time.monotonic()
    assert [item.method for item in captured] == ["GET"]


def test_capability_cache_does_not_cross_api_keys() -> None:
    from openrouter_recovery import get_model_capability

    client, captured = exchange()

    async def check():
        await get_model_capability(request(), RecoverySettings(mode="shadow"),
                                   api_key=KEY, client=client, deadline_seconds=1)
        await get_model_capability(request(), RecoverySettings(mode="shadow"),
                                   api_key="second-test-key", client=client, deadline_seconds=1)

    run(check())
    assert [item.method for item in captured] == ["GET", "GET"]


def test_unpriced_eligible_endpoint_blocks_all_routing() -> None:
    from openrouter_recovery import propose_recovery

    meta = metadata()
    expensive = dict(meta["data"]["endpoints"][0])
    expensive["pricing"] = {"prompt": "0.0000001", "completion": "0.0000002",
                            "image": "0.0001", "request": "0", "mystery_fee": "1"}
    meta["data"]["endpoints"].append(expensive)
    client, captured = exchange(meta=meta)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_unknown_even_zero_pricing_field_fails_closed() -> None:
    from openrouter_recovery import get_model_capability

    meta = metadata(pricing={"prompt": "0.0000002", "completion": "0.0000002",
                             "image": "0", "request": "0", "unknown_fee": "0"})
    client, _ = exchange(meta=meta)
    assert run(get_model_capability(request(), RecoverySettings(mode="shadow"),
                                    api_key=KEY, client=client)) is None


def test_changed_request_cannot_reuse_reserved_quote() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    client, captured = exchange()
    req = request()
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        changed = req.model_copy(update={"objective": "Different objective"})
        return await propose_recovery(changed, settings, api_key=KEY, client=client,
                                      reserved_capability=quote)

    result = run(check())
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_reduced_reserved_ceiling_cannot_send() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    client, captured = exchange()
    req = request()
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        reduced = replace(quote, worst_case_microusd=1)
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=reduced)

    result = run(check())
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_oversized_image_never_reaches_provider() -> None:
    from openrouter_recovery import get_model_capability

    req = request()
    large = req.image.model_copy(update={"data": TINY_PNG + b"0" * 65_536})
    req = req.model_copy(update={"image": large})
    client, captured = exchange()
    assert run(get_model_capability(req, RecoverySettings(mode="shadow"),
                                    api_key=KEY, client=client)) is None
    assert captured == []


def test_malformed_metadata_fails_closed_without_raising() -> None:
    from openrouter_recovery import propose_recovery

    meta = metadata()
    meta["data"]["architecture"]["input_modalities"] = [{"malformed": "image"}]
    client, captured = exchange(meta=meta)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_quote_over_incident_ceiling_blocks_completion() -> None:
    from openrouter_recovery import propose_recovery

    meta = metadata(pricing={"prompt": "0.00001", "completion": "0.00001",
                             "image": "0.01", "request": "0"})
    client, captured = exchange(meta=meta)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_nonzero_per_image_price_is_included_in_provider_cap() -> None:
    from openrouter_recovery import propose_recovery

    meta = metadata(pricing={"prompt": "0.0000001", "completion": "0.0000002",
                             "image": "0.0001", "request": "0"})
    client, captured = exchange(meta=meta)
    result = run(_preflight_propose(request(), RecoverySettings(mode="shadow"),
                                  api_key=KEY, client=client))
    assert result.error is None
    assert [item.method for item in captured] == ["GET", "POST"]
    body = json.loads(captured[-1].content)
    assert body["provider"]["max_price"]["image"] == 0.0001


def test_off_or_missing_key_never_fetches_metadata() -> None:
    from openrouter_recovery import propose_recovery

    client, captured = exchange()
    assert run(_preflight_propose(request(), RecoverySettings(), api_key=KEY, client=client)).error == "disabled"
    assert run(_preflight_propose(request(), RecoverySettings(mode="shadow"), api_key="", client=client)).error == "credentials_missing"
    assert captured == []


def test_real_default_metadata_produces_request_bound_affordable_quote() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    base_endpoint = {
        "model_id": MODEL, "context_length": 400000, "max_prompt_tokens": None,
        "max_completion_tokens": 128000,
        "supported_parameters": ["max_tokens", "response_format", "structured_outputs"],
        "pricing": {"prompt": "0.0000002", "completion": "0.00000125",
                    "input_cache_read": "0.00000002", "web_search": "0.01", "discount": 0},
    }
    flex = dict(base_endpoint, tag="openai/flex", pricing={
        "prompt": "0.0000001", "completion": "0.000000625",
        "input_cache_read": "0.00000001", "web_search": "0.01", "discount": 0,
    })
    azure = dict(base_endpoint, tag="azure", supported_parameters=[
        "max_completion_tokens", "response_format", "structured_outputs",
    ])
    meta = {"data": {"id": MODEL, "architecture": {"input_modalities": ["file", "image", "text"],
                        "output_modalities": ["text"]}, "endpoints": [
                            flex, dict(base_endpoint, tag="openai"), azure,
                        ]}}
    client, captured = exchange(meta=meta)
    req = request()
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        assert quote is not None
        assert quote.request_id == req.request_id
        assert 0 < quote.worst_case_microusd <= 50_000
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=quote)

    result = run(check())
    assert result.error is None
    body = json.loads(captured[-1].content)
    assert body["provider"]["only"] == ["openai", "openai/flex"]
    assert body["provider"]["max_price"]["prompt"] <= 0.201
    assert body["provider"]["max_price"]["completion"] <= 1.251
    assert body["provider"]["max_price"]["image"] == 0
    assert body["provider"]["max_price"]["request"] == 0


def test_expired_reserved_quote_never_sends_even_if_metadata_price_rises() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    client, captured = exchange()
    req = request()
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        expired = replace(quote, checked_at_monotonic=time.monotonic() - 301)
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=expired)

    result = run(check())
    assert result.error == "unsupported_model"
    assert [item.method for item in captured] == ["GET"]


def test_short_account_id_is_scrubbed_from_objective() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    req = request()
    req = req.model_copy(update={"objective": "Recover account 123",
                                 "context": req.context.model_copy(update={
                                     "scope": req.context.scope.model_copy(update={"account_id": "123"})})})
    client, captured = exchange()
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=quote)

    assert run(check()).error is None
    assert "123" not in captured[-1].content.decode()


def test_wrong_model_name_cannot_echo_account_id() -> None:
    from openrouter_recovery import get_model_capability, propose_recovery

    req = request()
    client, _ = exchange(reply=completion(model="openai/account-secret-314"))
    settings = RecoverySettings(mode="shadow")

    async def check():
        quote = await get_model_capability(req, settings, api_key=KEY, client=client)
        return await propose_recovery(req, settings, api_key=KEY, client=client,
                                      reserved_capability=quote)

    result = run(check())
    assert result.error == "model_mismatch"
    assert result.actual_model is None
    assert result.usage.prompt_tokens == 100
    assert result.cost_microusd == 1000


@pytest.mark.parametrize("flag", ["paused", "identity_conflict", "pending_transaction"])
def test_local_safety_gate_never_contacts_provider(flag: str) -> None:
    from openrouter_recovery import propose_recovery

    original = request()
    blocked = original.model_copy(update={
        "context": original.context.model_copy(update={flag: True}),
    })
    client, captured = exchange()
    result = run(_preflight_propose(blocked, RecoverySettings(mode="assist"),
                                  api_key=KEY, client=client))
    assert result.error == "disabled"
    assert captured == []
