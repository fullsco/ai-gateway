"""Finite loopback-only gateway fixture for real TCP SSE qualification."""

import asyncio
import sys

import httpx
import uvicorn

from gateway.app import create_app
from gateway.config import Settings
from tests.test_messages_api import make_runtime


class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        await asyncio.sleep(.02)
        yield b'{"input_tokens":1,"output_tokens":0}}}\n\n'
        await asyncio.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else .05)
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    async def aclose(self):
        pass


async def main():
    runtime, key = make_runtime(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Stream(),
    ))
    settings = Settings(_env_file=None, database_url=None, supabase_url=None, environment="test",
                        health_probe_enabled=False, max_in_flight_requests=2,
                        max_in_flight_per_client=2, stream_keepalive_seconds=.01,
                        log_level="CRITICAL")
    app = create_app(settings, runtime)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[1]),
                                         log_level="critical", access_log=False))
    # Synthetic test key is communicated through a pipe, never persisted as configuration.
    print(key, flush=True)
    try:
        async with asyncio.timeout(30):
            await server.serve()
    finally:
        await runtime.http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
