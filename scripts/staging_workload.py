import asyncio
from time import monotonic
from uuid import uuid4

import httpx

from scripts.staging_translated import (
    CALLS,
    PATHS,
    PROTOCOLS,
    RESULTS,
    SCENARIOS,
    continuation,
    decoded_calls,
    request_body,
    validate_json,
    validate_response,
)

SCENARIO_NAMES = ("json_two_turn", "sse_two_turn", "workflow_two_turn")


def coverage_key(protocol, scenario):
    return f"{protocol.value}/{scenario}"


def new_coverage():
    return {coverage_key(p, scenario): {"started": 0, "completed": 0, "overloaded": 0,
                                      "failed": 0, "failures": []}
            for p in PROTOCOLS for scenario in SCENARIO_NAMES}


def failure(row, error):
    row["failed"] += 1
    if len(row["failures"]) < 8:
        row["failures"].append(type(error).__name__)


async def interactive(http, headers, body, index, start, seconds, results, latencies,
                      status_latencies, coverage=None):
    number = index
    while monotonic() - start < seconds:
        base = f"http://127.0.0.1:{18330 if number % 4 < 2 else 18331}"
        row = None
        try:
            if coverage is not None:
                protocol, stream = SCENARIOS[number % len(SCENARIOS)]
                scenario = "sse_two_turn" if stream else "json_two_turn"
                row = coverage[coverage_key(protocol, scenario)]
                row["started"] += 1
                payload, path, turns = request_body(protocol, stream), PATHS[protocol], 2
            else:
                payload = {**body, "stream": number % 2 == 0,
                           "metadata": {"delay": .03 if number % 3 else .2}}
                if number % 5 == 0:
                    payload["messages"] = [{"role": "user", "content": "x" * 262144}]
                path, turns = "/v1/chat/completions", 1
            for turn in range(turns):
                began = monotonic()
                response = await http.post(base + path, headers=headers, json=payload)
                status = str(response.status_code)
                results[status] += 1
                elapsed = (monotonic() - began) * 1000
                latencies.append(elapsed)
                status_latencies.setdefault(status, []).append(elapsed)
                if response.status_code == 429:
                    if row is not None:
                        row["overloaded"] += 1
                    await asyncio.sleep(.05)
                    break
                if row is not None:
                    assistant = validate_response(protocol, response, stream, second=turn == 1)
                    if turn == 0:
                        payload = continuation(protocol, payload, assistant)
                    else:
                        row["completed"] += 1
                else:
                    assert response.status_code == 200
                    assert not payload["stream"] or "[DONE]" in response.text
        except (httpx.HTTPError, AssertionError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, httpx.HTTPError):
                results["transport_error"] += 1
            if row is None:
                if not isinstance(error, httpx.HTTPError):
                    raise
            else:
                failure(row, error)
                await asyncio.sleep(.05)
        number += 1


async def workflow_journey(http, headers, job_id, protocol):
    path = f"http://127.0.0.1:18330/v1/workflows/{job_id}"
    submitted = 0
    completed = False
    try:
        async with asyncio.timeout(25):
            while True:
                response = await http.get(path, headers=headers)
                assert response.status_code == 200
                state = response.json()
                if state["status"] == "completed":
                    assert submitted == len(CALLS), "workflow skipped tool results"
                    validate_json(protocol, state["result"], second=True)
                    completed = True
                    return
                assert state["status"] in {"queued", "running", "waiting_for_tool"}, (
                    "workflow did not complete")
                if state["status"] == "waiting_for_tool":
                    assert submitted < len(CALLS), "extra workflow tool request"
                    checkpoint = state["checkpoint"]
                    assert checkpoint["step"] == 1 and checkpoint["tool_calls"] == len(CALLS)
                    assert checkpoint["invocation_id"] == CALLS[submitted][0]
                    assert decoded_calls(protocol, checkpoint["pending_tools"]) == CALLS[submitted:]
                    assert decoded_calls(protocol, [checkpoint["tool"]]) == [CALLS[submitted]]
                    response = await http.post(path + "/tool-results", headers=headers, json={
                        "invocation_id": CALLS[submitted][0], "result": RESULTS[submitted]})
                    assert response.status_code == 200
                    submitted += 1
                await asyncio.sleep(.1)
    finally:
        if not completed:
            async def cancel():
                async with asyncio.timeout(5):
                    response = await http.post(path + "/cancel", headers=headers)
                    assert response.status_code == 200, "workflow cancellation failed"
                    response = await http.get(path, headers=headers)
                    assert response.json()["status"] in {
                        "cancelled", "completed", "failed", "expired"}
            task = asyncio.create_task(cancel())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise


async def workflows(http, headers, body, start, seconds, workflow_ids, responses, coverage=None):
    number = 0
    while monotonic() - start < seconds:
        row = None
        if coverage is not None:
            protocol = PROTOCOLS[number % len(PROTOCOLS)]
            row = coverage[coverage_key(protocol, "workflow_two_turn")]
            row["started"] += 1
            payload = {"protocol": protocol.value, "request": request_body(protocol),
                       "max_steps": 2, "max_tool_calls": len(CALLS), "max_cost": "1",
                       "max_total_tokens": 1024}
        else:
            payload = {"protocol": "openai_chat_completions",
                       "request": {**body, "metadata": {"delay": .1}}}
        try:
            response = await http.post("http://127.0.0.1:18330/v1/workflows",
                headers={**headers, "Idempotency-Key": str(uuid4())}, json=payload)
            responses[str(response.status_code)] += 1
            assert response.status_code in {202, 409, 429}
            if response.status_code == 202:
                job_id = response.json()["id"]
                workflow_ids.append(job_id)
                if row is not None:
                    await workflow_journey(http, headers, job_id, protocol)
                    row["completed"] += 1
            elif row is not None:
                row["overloaded"] += 1
        except (httpx.HTTPError, AssertionError, ValueError, KeyError, TypeError,
                TimeoutError) as error:
            if row is None:
                raise
            failure(row, error)
        number += 1
        await asyncio.sleep(.5)
