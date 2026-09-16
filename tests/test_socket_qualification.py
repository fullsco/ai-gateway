import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_fragmented_sse_over_real_tcp_and_disconnect_capacity_recovery():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "scripts/dev_check.py", "--module", "tests.socket_gateway_probe", str(port),
        cwd=Path(__file__).resolve().parents[1], stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        key = (await asyncio.wait_for(process.stdout.readline(), 10)).decode().strip()
        assert key
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5,
                                    trust_env=False) as client:
            for _ in range(50):
                try:
                    if (await client.get("/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)
            else:
                pytest.fail("Loopback gateway did not become ready")
            payload = {"model": "model-x", "messages": [], "max_tokens": 10, "stream": True}
            headers = {"x-api-key": key}
            async with client.stream(
                "POST", "/v1/messages", json=payload, headers=headers,
            ) as response:
                assert response.status_code == 200
                chunks = []
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    await asyncio.sleep(.01)  # Bounded slow reader.
                body = b"".join(chunks)
                assert b'"usage":{"input_tokens":1,"output_tokens":0}' in body
                assert b"event: message_stop" in body
            # Close mid-event; upstream cleanup must return both admission and route slots.
            async with client.stream(
                "POST", "/v1/messages", json=payload, headers=headers,
            ) as response:
                assert response.status_code == 200
                await anext(response.aiter_bytes())
            await asyncio.sleep(.1)
            recovered = await client.post("/v1/messages", json=payload, headers=headers)
            assert recovered.status_code == 200
            assert (await client.get("/health")).status_code == 200
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
