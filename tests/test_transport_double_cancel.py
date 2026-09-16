"""Regression: repeated ASGI cancellation must close the actual upstream TCP socket."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from gateway.api.executor import _stream_response
from gateway.observability import StreamUsageAccumulator
from gateway.protocols import ClientProtocol
from gateway.protocols.stream_completion import StreamCompletion


@pytest.mark.asyncio
@pytest.mark.parametrize("keepalive", [0, .01])
async def test_double_cancel_drains_reader_once_and_closes_upstream_socket(keepalive):
    peer_closed = asyncio.Event()
    started = asyncio.Event()

    async def peer(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            body = b'data: {"choices":[]}\n\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n"
                         + f"{len(body):x}\r\n".encode() + body + b"\r\n")
            await writer.drain()
            await reader.read()
            peer_closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, httpx.AsyncClient(trust_env=False) as client:
        response = await client.send(client.build_request("GET", f"http://127.0.0.1:{port}"),
                                     stream=True)
        iterator = response.aiter_raw()
        first = await anext(iterator)

        async def finish(completed, *, error=None):
            await response.aclose()

        finalizer = SimpleNamespace(
            usage=StreamUsageAccumulator(ClientProtocol.OPENAI_CHAT_COMPLETIONS),
            completion=StreamCompletion(ClientProtocol.OPENAI_CHAT_COMPLETIONS),
            client_protocol=ClientProtocol.OPENAI_CHAT_COMPLETIONS, finish=finish,
        )

        async def consume():
            async for _ in _stream_response(
                first, iterator, finalizer, keepalive_seconds=keepalive,
            ):
                started.set()

        task = asyncio.create_task(consume())
        try:
            await started.wait()
            await asyncio.sleep(.02)
            task.cancel()
            asyncio.get_running_loop().call_soon(task.cancel)
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(peer_closed.wait(), 2)
            await asyncio.wait_for(finalizer._cleanup_task, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await response.aclose()
