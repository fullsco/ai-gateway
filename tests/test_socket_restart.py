"""Finite loopback rolling-replacement drill; no database daemon or real providers."""

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_server(port):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "scripts/dev_check.py", "--module", "tests.socket_gateway_probe",
        str(port), "1", cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    key = (await asyncio.wait_for(process.stdout.readline(), 10)).decode().strip()
    assert key
    return process, key


async def ready(client):
    for _ in range(60):
        try:
            if (await client.get("/ready")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(.05)
    pytest.fail("Loopback replacement failed readiness")


@pytest.mark.asyncio
async def test_old_process_finishes_active_stream_while_replacement_serves_new_requests():
    processes = []
    first_port, second_port = free_port(), free_port()
    while second_port == first_port:
        second_port = free_port()
    try:
        first, first_key = await start_server(first_port)
        processes.append(first)
        second, second_key = await start_server(second_port)
        processes.append(second)
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{first_port}", timeout=8,
                              trust_env=False) as old,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{second_port}", timeout=8,
                              trust_env=False) as replacement,
        ):
            await ready(old)
            await ready(replacement)
            body = {"model": "model-x", "messages": [], "stream": True, "max_tokens": 10}
            async with old.stream("POST", "/v1/messages", json=body,
                                  headers={"x-api-key": first_key}) as response:
                assert response.status_code == 200
                iterator = response.aiter_bytes()
                initial = await anext(iterator)
                first.terminate()
                new_request = asyncio.create_task(replacement.post(
                    "/v1/messages", json=body, headers={"x-api-key": second_key},
                ))
                received = initial + b"".join([chunk async for chunk in iterator])
                assert b"event: message_stop" in received
                assert b"upstream_truncated" not in received
                new_response = await new_request
                assert new_response.status_code == 200
                assert b"event: message_stop" in new_response.content
            await asyncio.wait_for(first.wait(), 10)
            assert (await replacement.get("/health")).status_code == 200
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
