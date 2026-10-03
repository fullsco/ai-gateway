"""Isolated release-drill policy, artifact provenance and bounded synthetic provider."""

import asyncio
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

PORTS = {"ingress": 18520, "baseline": 18521, "current": 18522,
         "provider": 18523, "jobs": 18524}
DATABASE_PATTERN = re.compile(r"gateway_release_drill_[0-9a-f]{24}\Z")


def release_dsn(dsn):
    try:
        parsed = urlsplit(dsn)
        allowed = (parsed.scheme in {"postgres", "postgresql"}
                   and parsed.hostname == "127.0.0.1" and parsed.port == 5432
                   and parsed.username == "gateway_qualification"
                   and DATABASE_PATTERN.fullmatch(parsed.path.removeprefix("/"))
                   and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        raise ValueError("Release drill requires its own isolated database")
    return dsn


def provenance(manifest, label):
    import gateway

    root = Path(gateway.__file__).resolve().parent
    prefix = Path(sys.prefix).resolve()
    if not root.is_relative_to(prefix) or "site-packages" not in root.parts:
        raise RuntimeError("Gateway must come from this release's installed wheel")
    expected = manifest["versions"][label]
    actual = {f"gateway/{path.relative_to(root)}": hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob("*.py")}
    if actual != expected["gateway_files"]:
        raise RuntimeError("Installed release code does not match artifact manifest")
    dependencies = {d.metadata["Name"].lower(): d.version
                    for d in importlib.metadata.distributions()
                    if d.metadata["Name"].lower() not in {"ai-gateway", "pip", "setuptools"}}
    return {"revision": expected["revision"], "wheel_sha256": expected["wheel_sha256"],
            "gateway_root": str(root), "python": sys.version.split()[0],
            "dependency_sha256": hashlib.sha256(json.dumps(
                dependencies, sort_keys=True).encode()).hexdigest(), "gateway_files": len(actual)}


def ingress_config(directory, rolled_back=False):
    target = PORTS["baseline" if rolled_back else "current"]
    submission = "return 503;" if rolled_back else f"proxy_pass http://127.0.0.1:{target};"
    return f"""pid {directory}/nginx.pid;
error_log {directory}/nginx-error.log warn;
worker_processes 1;
worker_shutdown_timeout 20s;
events {{ worker_connections 64; }}
http {{
    access_log off;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_set_header Connection \"\";
    proxy_read_timeout 30s;
    proxy_connect_timeout 3s;
    client_max_body_size 1m;
    server {{
        listen 127.0.0.1:{PORTS['ingress']};
        location = /v1/workflows {{ {submission} }}
        location ^~ /v1/workflows/ {{ proxy_pass http://127.0.0.1:{PORTS['jobs']}; }}
        location / {{ proxy_pass http://127.0.0.1:{target}; }}
    }}
}}
"""


class ReleaseIdentity:
    def __init__(self, app, label, *, jobs_only=False):
        self.app, self.label, self.jobs_only = app, label, jobs_only

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.jobs_only:
            path = scope["path"]
            if not (path.startswith("/v1/workflows/") or path in {
                "/health", "/ready", "/qualification/provenance",
            }):
                from starlette.responses import JSONResponse

                await JSONResponse({"error": "release_draining"}, status_code=503)(
                    scope, receive, send)
                return

        async def identified(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                           (b"x-qualification-release", self.label.encode())]}
            await send(message)
        await self.app(scope, receive, identified)


def provider_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(docs_url=None, redoc_url=None)
    state = {"requests": {}, "active": 0}
    release = asyncio.Event()

    @app.get("/health")
    async def health():
        return state

    @app.post("/release-stream")
    async def release_stream():
        release.set()
        return {"released": True}

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        body = await request.json()
        messages = body.get("messages", [])
        marker = messages[0].get("content", "") if messages else ""
        if marker not in {"release:warmup", "release:stream", "release:baseline",
                          "release:recovered", "release:tool", "release:queued"}:
            return JSONResponse({"error": "Unknown synthetic workload"}, status_code=422)
        second = marker == "release:tool" and len(messages) > 1
        key = marker + (":final" if second else ":initial")
        state["requests"][key] = state["requests"].get(key, 0) + 1
        message = {"role": "assistant", "content": "release-ok"}
        finish = "stop"
        if marker == "release:tool":
            if second:
                if (len(messages) != 3 or messages[-1].get("tool_call_id") != "release-call"
                        or json.loads(messages[-1].get("content", "null")) != {"answer": 14}):
                    return JSONResponse({"error": "Bad synthetic continuation"}, status_code=422)
            else:
                message.update(content=None, tool_calls=[{"id": "release-call", "type": "function",
                    "function": {"name": "double", "arguments": '{"value":7}'}}])
                finish = "tool_calls"
        full = {"id": "release-synthetic", "object": "chat.completion", "model": "synthetic",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}
        if body.get("stream"):
            state["active"] += 1

            async def chunks():
                try:
                    yield b'data: {"choices":[{"index":0,"delta":{"content":"release-ok"}}]}\n\n'
                    async with asyncio.timeout(25):
                        await release.wait()
                    yield (b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
                           b'"usage":{"prompt_tokens":4,"completion_tokens":1}}\n\n')
                    yield b"data: [DONE]\n\n"
                finally:
                    state["active"] -= 1
            return StreamingResponse(chunks(), media_type="text/event-stream")
        return full
    return app
