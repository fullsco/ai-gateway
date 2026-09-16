"""Request-owned objects must be released by refcount, without a cyclic-GC pass."""

import asyncio
import gc
import weakref
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from gateway.admission import InferenceAdmissionMiddleware
from gateway.distributed_capacity import (
    DistributedCapacity,
    capacity_lifetime,
    lifetime_owner,
    owned_leases,
    owned_probes,
    owned_work,
)
from gateway.middleware import RequestContextMiddleware


class Scope(dict):
    pass


class Payload:
    pass


@pytest.fixture
def no_cyclic_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def settings():
    return SimpleNamespace(
        max_in_flight_requests=2, max_request_body_bytes=1024,
        max_in_flight_body_bytes=2048, request_body_timeout_seconds=0.01,
        overload_retry_after_seconds=1, max_in_flight_per_client=2,
        max_in_flight_streams=2, request_id_header="x-request-id",
        trust_incoming_request_id=False, trace_sample_rate=0,
    )


def scope():
    return Scope(
        type="http", method="POST", path="/v1/messages", raw_path=b"/v1/messages",
        root_path="", query_string=b"", headers=[], scheme="http",
        http_version="1.1", asgi={"version": "3.0", "spec_version": "2.3"},
        server=("test", 80), client=("test", 123), state={"payload": Payload()},
    )


async def settle():
    # Task wakeups/gather callbacks may retain frames for a turn; never collect GC.
    for _ in range(3):
        await asyncio.sleep(0)


class Capacity(DistributedCapacity):
    def __init__(self, events, fail_release=False):
        super().__init__(None)
        self.events = events
        self.fail_release = fail_release

    async def release(self, owner):
        self.events.append(owner)
        await asyncio.sleep(0)
        if self.fail_release:
            raise RuntimeError("release failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [
    "success", "cancel", "failure", "reject", "disconnect", "timeout", "receive_failure",
    "release_failure",
])
async def test_admission_detaches_state_on_every_exit(no_cyclic_gc, ending):
    refs, states, released = {}, [], []

    async def downstream(request_scope, receive, send):
        assert request_scope["state"]["admit_client"]("client", True)
        manager = Capacity(released, ending == "release_failure")
        refs["manager"] = weakref.ref(manager)
        request_scope["state"]["start_distributed_admission"](manager, "admission", 2)
        await receive()
        if ending == "failure":
            raise RuntimeError("dispatch failed")
        if ending == "cancel":
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    app = InferenceAdmissionMiddleware(downstream, settings())

    async def run():
        request_scope = scope()
        states.append(request_scope["state"])
        refs["scope"] = weakref.ref(request_scope)
        refs["payload"] = weakref.ref(request_scope["state"]["payload"])
        if ending == "reject":
            request_scope["headers"] = [(b"content-length", b"2048")]

        async def receive():
            if ending == "disconnect":
                return {"type": "http.disconnect"}
            if ending == "timeout":
                await asyncio.Event().wait()
            if ending == "receive_failure":
                raise OSError("upload failed")
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            pass

        try:
            await app(request_scope, receive, send)
        except (RuntimeError, OSError, asyncio.CancelledError):
            return "raised"
        return "returned"

    outcome = await asyncio.create_task(run())
    assert outcome == ("raised" if ending in {"cancel", "failure", "receive_failure"}
                       else "returned")
    assert set(states[0]) == {"payload"}
    states.clear()
    await settle()
    assert not gc.isenabled()
    assert all(ref() is None for ref in refs.values()), refs
    assert released == (["admission"] if "manager" in refs else [])
    assert (app._in_flight, app._body_bytes, app._streams, app._clients) == (0, 0, 0, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["join", "release"])
async def test_admission_detaches_state_before_cleanup_is_interrupted(no_cyclic_gc, stage):
    refs, states = [], []
    cleanup_started = asyncio.Event()

    class BlockedCapacity(Capacity):
        def guard(self, owner, count, task=None):
            async def pulse():
                try:
                    await asyncio.Event().wait()
                finally:
                    if stage == "join":
                        cleanup_started.set()
                        await asyncio.Event().wait()

            return asyncio.create_task(pulse())

        async def release(self, owner):
            cleanup_started.set()
            await asyncio.Event().wait()

    async def downstream(request_scope, receive, send):
        manager = BlockedCapacity([])
        refs.append(weakref.ref(manager))
        request_scope["state"]["start_distributed_admission"](manager, "admission", 1)
        await asyncio.sleep(0)

    app = InferenceAdmissionMiddleware(downstream, settings())

    async def run():
        request_scope = scope()
        states.append(request_scope["state"])
        refs.append(weakref.ref(request_scope))

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            pass

        try:
            await app(request_scope, receive, send)
        except asyncio.CancelledError:
            return "cancelled"

    task = asyncio.create_task(run())
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert set(states[0]) == {"payload"}
    task.cancel()
    assert await task == "cancelled"
    del task
    states.clear()
    await settle()
    assert all(ref() is None for ref in refs)
    assert (app._in_flight, app._body_bytes) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "cancel", "failure", "release_failure"])
async def test_capacity_lists_release_copied_contexts_without_gc(no_cyclic_gc, ending):
    refs, released = {}, []

    async def run():
        try:
            async with capacity_lifetime():
                manager = Capacity(released, ending == "release_failure")
                probe_manager = Capacity(released, ending == "release_failure")
                pulse = manager.guard("lease", 1)
                probe = probe_manager.guard("probe", 1)

                async def release_probe():
                    await probe_manager.release("probe")

                owned_leases.get().append((manager, "lease", pulse))
                owned_probes.get().append((probe, release_probe))
                work = asyncio.create_task(asyncio.sleep(0))
                owned_work.get().append(work)
                await work
                refs.update({name: weakref.ref(value) for name, value in (
                    ("manager", manager), ("probe_manager", probe_manager),
                    ("pulse", pulse), ("probe", probe), ("work", work),
                )})
                if ending == "failure":
                    raise RuntimeError("dispatch failed")
                if ending == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
        except (RuntimeError, asyncio.CancelledError):
            return "raised"
        return "returned"

    outcome = await asyncio.create_task(run())
    assert outcome == ("raised" if ending in {"cancel", "failure"} else "returned")
    await settle()
    assert not gc.isenabled()
    assert all(ref() is None for ref in refs.values()), refs
    assert released == ["probe", "lease"]
    assert owned_leases.get() is owned_probes.get() is owned_work.get() is None
    assert lifetime_owner.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["join", "probe", "lease"])
async def test_interrupted_cleanup_still_clears_all_lifetime_lists(no_cyclic_gc, stage):
    lists, refs = [], []
    release_started = asyncio.Event()

    class BlockedCapacity(Capacity):
        async def release(self, owner):
            assert stage == "lease"
            release_started.set()
            await asyncio.Event().wait()

    async def probe_pulse():
        try:
            await asyncio.Event().wait()
        finally:
            if stage == "join":
                release_started.set()
                await asyncio.Event().wait()

    async def release_probe():
        if stage == "probe":
            release_started.set()
            await asyncio.Event().wait()

    async def run():
        try:
            async with capacity_lifetime():
                manager = BlockedCapacity([])
                pulse = manager.guard("lease", 1)
                owned_leases.get().append((manager, "lease", pulse))
                probe = asyncio.create_task(probe_pulse())
                owned_probes.get().append((probe, release_probe))
                work = asyncio.create_task(asyncio.sleep(0))
                owned_work.get().append(work)
                await work
                lists.extend((owned_leases.get(), owned_probes.get(), owned_work.get()))
                refs.extend(weakref.ref(value) for value in (manager, pulse, probe, work))
        except asyncio.CancelledError:
            return "cancelled"

    task = asyncio.create_task(run())
    await asyncio.wait_for(release_started.wait(), 1)
    task.cancel()
    assert await task == "cancelled"
    del task
    await settle()
    assert lists == [[], [], []]
    assert all(ref() is None for ref in refs)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending,spec_version", [
    ("success", "2.3"), ("cancel", "2.3"), ("disconnect", "2.3"),
    ("success", "2.4"), ("cancel", "2.4"), ("failure", "2.4"),
    ("failure", "2.3"),
])
async def test_streaming_response_stack_releases_request_without_gc(
    no_cyclic_gc, ending, spec_version,
):
    refs, released, bodies = {}, [], []
    first_body, finish, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api = FastAPI()

    @api.post("/v1/messages")
    async def route(request: Request):
        await request.json()
        manager = Capacity(released)
        refs["manager"] = weakref.ref(manager)
        pulse = manager.guard("route", 1)
        refs["pulse"] = weakref.ref(pulse)
        owned_leases.get().append((manager, "route", pulse))
        request.state.start_distributed_admission(manager, "admission", 2)

        async def events():
            yield b"data: first\n\n"
            await finish.wait()
            if ending == "failure":
                raise RuntimeError("stream failed")
            yield b"data: final\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    admission = InferenceAdmissionMiddleware(api, settings())
    app = RequestContextMiddleware(admission, settings())

    async def run():
        request_scope = scope()
        request_scope["asgi"]["spec_version"] = spec_version
        refs["scope"] = weakref.ref(request_scope)
        refs["payload"] = weakref.ref(request_scope["state"]["payload"])
        uploaded = False

        async def receive():
            nonlocal uploaded
            if not uploaded:
                uploaded = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                bodies.append(message.get("body", b""))
                first_body.set()

        try:
            await app(request_scope, receive, send)
        except (RuntimeError, asyncio.CancelledError):
            return "raised"
        return "returned"

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(first_body.wait(), 1)
        assert admission._in_flight == 1
        if ending == "cancel":
            task.cancel()
        elif ending == "disconnect":
            disconnect.set()
        else:
            finish.set()
        outcome = await asyncio.wait_for(task, 1)
        assert outcome == ("raised" if ending in {"failure", "cancel"} else "returned")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    del task
    await settle()
    assert released == ["route", "admission"]
    assert (admission._in_flight, admission._body_bytes) == (0, 0)
    assert bodies[0] == b"data: first\n\n"
    assert not gc.isenabled()
    if ending == "failure" and spec_version == "2.3":
        # AnyIO's exception-group traceback is cyclic: verify it is collectible,
        # rather than requiring reference-count-only reclamation from third-party code.
        gc.collect()
    assert all(ref() is None for ref in refs.values()), refs
