import asyncio
from copy import deepcopy

import pytest

from gateway.admission import InferenceAdmissionMiddleware
from gateway.api.input_limits import payload_error
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry
from gateway.protocols import Capability, ClientProtocol, normalize_request
from tests.test_admission import Exchange, echo


def limits(**kwargs):
    return Settings(_env_file=None, database_url=None, **kwargs)


def check(payload, *, capabilities=frozenset(), window=None, **kwargs):
    registry = ModelRegistry([CanonicalModel("test", frozenset(), capabilities,
                                            context_window=window)], [])
    normalized = normalize_request(ClientProtocol.OPENAI_CHAT_COMPLETIONS, payload)
    return payload_error(normalized, registry, limits(**kwargs))


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, "100", 32769])
def test_output_bounds_are_strict_for_every_protocol_field(key, value):
    assert check({"model": "test", key: value})


def test_model_context_is_estimated_and_input_is_never_modified():
    payload = {"model": "test", "messages": [{"role": "user", "content": "x" * 500}],
               "max_completion_tokens": 20}
    before = deepcopy(payload)
    assert check(payload, window=100)
    assert check(payload, window=1000) is None
    assert payload == before


def test_tool_results_and_multimodal_have_distinct_byte_limits():
    assert check({"model": "test", "tools": [{}, {}]}, max_tools=1)
    assert check({"model": "test", "messages": [{"role": "tool", "content": "x" * 100}]},
                 max_tool_payload_bytes=50)
    media = {"model": "test", "input": [{"type": "input_image", "image_url": "x" * 100}]}
    assert check(media)  # No vision capability.
    assert check(media, capabilities=frozenset({Capability.VISION})) is None
    assert check(media, capabilities=frozenset({Capability.VISION}), max_multimodal_bytes=50)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["done", "cancel", "error"])
async def test_client_and_stream_limits_hold_until_asgi_exit_and_recover(ending):
    ready, finish = asyncio.Event(), asyncio.Event()

    async def downstream(scope, receive, send):
        client = dict(scope["headers"]).get(b"client", b"a").decode()
        stream = dict(scope["headers"]).get(b"stream", b"yes") == b"yes"
        admitted = scope["state"]["admit_client"](client, stream)
        if not admitted:
            await send({"type": "http.response.start", "status": 429, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await echo(scope, receive, send)
        if client == "a":
            ready.set()
            await finish.wait()
            if ending == "error":
                raise RuntimeError("synthetic failure")

    app = InferenceAdmissionMiddleware(downstream, limits(
        max_in_flight_requests=4, max_in_flight_per_client=1, max_in_flight_streams=1,
    ))
    task = asyncio.create_task(Exchange("/v1/messages").run(app))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        for headers in ([], [(b"client", b"b")]):
            rejected = Exchange("/v1/messages", headers=headers)
            await rejected.run(app)
            assert rejected.status == 429
        short = Exchange("/v1/responses", headers=[(b"client", b"b"), (b"stream", b"no")])
        await short.run(app)
        assert short.status == 200
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "error":
            finish.set()
            with pytest.raises(RuntimeError):
                await task
        else:
            finish.set()
            await task
        assert app._clients == {}
        assert app._streams == app._body_bytes == app._in_flight == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
