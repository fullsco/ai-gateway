"""Single bounded workflow step; external tools always pause for caller-supplied results."""

import asyncio
import base64
import json
import random
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from gateway.api.executor import execute_request
from gateway.api.input_limits import payload_error
from gateway.artifacts import ArtifactStore
from gateway.context import reset_request_id, set_request_id
from gateway.distributed_capacity import capacity_lifetime
from gateway.protocols import ClientProtocol, normalize_request
from gateway.workflow_context import active_workflow
from gateway.workflow_store import LeaseLost, WorkflowStore, encode


def prepare_request(job):
    checkpoint = job["checkpoint"]
    payload = deepcopy(checkpoint.get("request", job["input"]["request"]))
    protocol = ClientProtocol(job["input"]["protocol"])
    if "tool_result" in checkpoint:
        result = checkpoint["tool_result"]
        text = result if isinstance(result, str) else json.dumps(result)
        invocation = checkpoint["invocation_id"]
        if protocol == ClientProtocol.ANTHROPIC_MESSAGES:
            payload.setdefault("messages", []).append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": invocation, "content": text},
            ]})
        elif protocol == ClientProtocol.OPENAI_CHAT_COMPLETIONS:
            payload.setdefault("messages", []).append({"role": "tool", "tool_call_id": invocation,
                                                       "content": text})
        else:
            payload.setdefault("input", []).append({"type": "function_call_output",
                                                    "call_id": invocation, "output": text})
    payload["stream"] = False
    return protocol, payload


def checkpoint_for_tools(protocol, payload, response, step):
    request = deepcopy(payload)
    if protocol == ClientProtocol.ANTHROPIC_MESSAGES:
        content = response.get("content", [])
        calls = [part for part in content
                 if isinstance(part, dict) and part.get("type") == "tool_use"]
        request.setdefault("messages", []).append({"role": "assistant", "content": content})
    elif protocol == ClientProtocol.OPENAI_CHAT_COMPLETIONS:
        choices = response.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        calls = message.get("tool_calls", [])
        request.setdefault("messages", []).append(message)
    else:
        output = response.get("output", [])
        calls = [part for part in output if part.get("type") == "function_call"]
        if isinstance(request.get("input"), str):
            request["input"] = [{"role": "user", "content": request["input"]}]
        request.setdefault("input", []).extend(output)
    if not calls:
        return None
    identifiers = [call.get("call_id", call.get("id")) for call in calls]
    if any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in identifiers):
        raise ValueError("Provider returned invalid tool invocation IDs")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Provider returned duplicate tool invocation IDs")
    # Keep every invocation; request one result at a time, without re-dispatching inference.
    first = calls[0]
    return {"step": step, "request": request, "pending_tools": calls,
            "invocation_id": first.get("call_id", first.get("id")), "tool": first}


class WorkflowWorker:
    def __init__(self, pool, runtime, settings, *, owner=None,
                 live_state=None, health_recorder=None):
        self.store = WorkflowStore(pool, per_client_capacity=getattr(
            settings, "workflow_per_client_capacity", 1,
        ))
        self.pool, self.runtime, self.settings = pool, runtime, settings
        self.owner = owner or uuid4()
        self.live_state = live_state
        self.health_recorder = health_recorder

    async def run_once(self):
        await self.store.recover()
        job = await self.store.claim(self.owner)
        if job is None:
            return False
        task = asyncio.create_task(self._execute(job))

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                # Renewal cannot hang past the execution lease's remaining safety margin.
                async with asyncio.timeout(10):
                    await self.store.renew(job["id"], self.owner, job["version"])

        pulse = asyncio.create_task(heartbeat())
        try:
            remaining_seconds = max(0, (job["deadline"] - datetime.now(UTC)).total_seconds())
            done, _ = await asyncio.wait({task, pulse}, timeout=remaining_seconds,
                                          return_when=asyncio.FIRST_COMPLETED)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return True
            if pulse in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await pulse
            else:
                await task
        except LeaseLost:
            pass  # Cancellation/expired ownership must never commit a stale result.
        finally:
            task.cancel()
            pulse.cancel()
            await asyncio.gather(task, pulse, return_exceptions=True)
        return True

    async def _execute(self, job):
        checkpoint = job["checkpoint"]
        step = checkpoint.get("step", 0)
        token = set_request_id(str(uuid4()))
        workflow_token = active_workflow.set((job["id"], self.owner, job["version"]))
        try:
            artifacts = None
            if self.settings.artifact_encryption_key:
                artifacts = ArtifactStore(self.pool, base64.b64decode(
                    self.settings.artifact_encryption_key, validate=True,
                ))
            if checkpoint.get("request_artifact"):
                if artifacts is None:
                    raise ValueError("Artifact storage is required for this checkpoint")
                content = await artifacts.get(job["client_id"], job["id"],
                                              UUID(checkpoint["request_artifact"]["id"]))
                if content is None:
                    raise ValueError("Workflow request artifact expired")
                checkpoint = {**checkpoint, "request": json.loads(content)}
                job = {**job, "checkpoint": checkpoint}
            tool_result = checkpoint.get("tool_result")
            if isinstance(tool_result, dict) and set(tool_result) == {"artifact_id"}:
                if artifacts is None:
                    raise ValueError("Artifact storage is required for this tool result")
                content = await artifacts.get(job["client_id"], job["id"],
                                              UUID(tool_result["artifact_id"]))
                if content is None:
                    raise ValueError("Tool result artifact expired")
                checkpoint = {**checkpoint, "tool_result": content.decode("utf-8")}
                job = {**job, "checkpoint": checkpoint}
            protocol, payload = prepare_request(job)
            client = self.runtime.key_store.get_client(str(job["client_id"]))
            if (client is None or not client.enabled
                    or not client.permissions.permits(protocol, payload.get("model", ""))):
                raise ValueError("Workflow permission revoked")
            remaining = (
                checkpoint.get("pending_tools", [])[1:] if "tool_result" in checkpoint else []
            )
            if remaining:
                next_tool = remaining[0]
                next_checkpoint = {"request": payload, "step": step, "pending_tools": remaining,
                     "tool_calls": checkpoint.get("tool_calls", 0),
                     "invocation_id": next_tool.get("call_id", next_tool.get("id")),
                     "tool": next_tool}
                next_checkpoint = await self.externalize(job, next_checkpoint, artifacts)
                await self.store.transition(job["id"], self.owner, job["version"],
                                            "waiting_for_tool", next_checkpoint)
                return
            if step >= job["input"]["max_steps"]:
                raise ValueError("Workflow step limit reached")
            # Explicit output ceiling is part of each workflow's cumulative reservation.
            output_fields = ("max_tokens", "max_completion_tokens", "max_output_tokens")
            if not any(field in payload for field in output_fields):
                field = "max_output_tokens" if protocol == ClientProtocol.OPENAI_RESPONSES \
                    else "max_tokens"
                payload[field] = min(4096, self.settings.max_output_tokens)
            normalized = normalize_request(protocol, payload)
            error = payload_error(normalized, self.runtime.model_registry, self.settings)
            if error:
                raise ValueError(error)
            for capacity_retry in range(8):
                async with capacity_lifetime():
                    response = await execute_request(normalized, {}, self.runtime, self.settings,
                        client_id=str(job["client_id"]), db_pool=self.pool,
                        live_state=self.live_state, health_recorder=self.health_recorder)
                # Only executor-generated proof of zero dispatch allows an automatic
                # retry. An upstream 429/503 or an uncertain attempt still pauses.
                if (response.status_code != 429
                        or getattr(response, "_gateway_capacity_deferred", False) is not True
                        or capacity_retry == 7):
                    break
                await asyncio.sleep(min(1, .1 * 2 ** capacity_retry) * random.uniform(.5, 1))
            if response.status_code != 200:
                # A completed/unknown upstream attempt is not automatically replayed.
                checkpoint = {key: value for key, value in checkpoint.items()
                              if key not in {"tool_result", "pending_tools", "invocation_id",
                                             "tool", "request_artifact"}}
                checkpoint = await self.externalize(job, {
                    **checkpoint, "request": payload, "reason": "inference_failed",
                    "http_status": response.status_code,
                }, artifacts)
                await self.store.transition(job["id"], self.owner, job["version"],
                                            "waiting_for_approval", checkpoint)
                return
            result = json.loads(response.body)
            if protocol == ClientProtocol.OPENAI_RESPONSES and result.get("status") in {
                "queued", "in_progress", "incomplete", "failed", "cancelled",
            }:
                raise ValueError("Provider returned a non-completed background response")
            tools = checkpoint_for_tools(protocol, payload, result, step + 1)
            tool_calls = checkpoint.get("tool_calls", 0)
            if tools:
                tool_calls += len(tools["pending_tools"])
                if tool_calls > job["input"].get("max_tool_calls", 16):
                    raise ValueError("Workflow tool-call limit reached")
                tools["tool_calls"] = tool_calls
                tools = await self.externalize(job, tools, artifacts)
            elif len(encode(result).encode()) > 256 * 1024:
                if artifacts is None:
                    raise ValueError("Large workflow output requires artifact storage")
                reference = await artifacts.put(
                    job["client_id"], job["id"], encode(result).encode(),
                )
                result = {"artifact": reference, "content_type": "application/json"}
            await self.store.transition(job["id"], self.owner, job["version"],
                "waiting_for_tool" if tools else "completed", tools or {"step": step + 1},
                None if tools else result)
        except LeaseLost:
            raise
        except Exception as exc:
            await self.store.transition(job["id"], self.owner, job["version"], "failed",
                                        {"step": step, "error_type": type(exc).__name__})
        finally:
            active_workflow.reset(workflow_token)
            reset_request_id(token)

    async def externalize(self, job, checkpoint, artifacts):
        if len(encode(checkpoint).encode()) <= 256 * 1024:
            return checkpoint
        if artifacts is None:
            raise ValueError("Large workflow checkpoint requires artifact storage")
        result = dict(checkpoint)
        request = result.pop("request")
        result["request_artifact"] = await artifacts.put(
            job["client_id"], job["id"], encode(request).encode(),
        )
        return result
