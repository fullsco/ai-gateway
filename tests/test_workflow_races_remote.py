import asyncio
from uuid import uuid4

import pytest

from gateway.workflow_store import LeaseLost, WorkflowConflict, WorkflowStore
from scripts.seed_dbtest import CLIENT
from tests.test_remote_isolation import dedicated_pool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_cancellation_racing_worker_completion_has_one_terminal_outcome():
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool)
        job = await store.submit(CLIENT, str(uuid4()), {"synthetic": True})
        owner = uuid4()
        try:
            claimed = await store.claim(owner)
            results = await asyncio.gather(
                store.cancel(CLIENT, job["id"]),
                store.transition(job["id"], owner, claimed["version"],
                                 "completed", {}, {"ok": True}),
                return_exceptions=True,
            )
            assert all(not isinstance(result, Exception) or isinstance(result, LeaseLost)
                       for result in results)
            final = await store.get(CLIENT, job["id"])
            assert final["status"] in {"completed", "cancelled"}
            events = await store.events(CLIENT, job["id"], -1)
            terminal = [event for event in events if event["kind"] in {"completed", "cancelled"}]
            assert len(terminal) == 1
            assert terminal[0]["kind"] == final["status"]
            assert await store.claim(uuid4()) is None
        finally:
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


async def test_duplicate_tool_results_racing_are_idempotent_and_conflicts_are_rejected():
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool)
        job = await store.submit(CLIENT, str(uuid4()), {"synthetic": True})
        owner = uuid4()
        try:
            claimed = await store.claim(owner)
            await store.transition(job["id"], owner, claimed["version"], "waiting_for_tool",
                                   {"invocation_id": "synthetic-tool"})
            responses = await asyncio.gather(*(
                store.tool_result(CLIENT, job["id"], "synthetic-tool", {"answer": 42})
                for _ in range(2)
            ))
            assert responses[0]["version"] == responses[1]["version"]
            with pytest.raises(WorkflowConflict):
                await store.tool_result(CLIENT, job["id"], "synthetic-tool", {"answer": 43})
            events = await store.events(CLIENT, job["id"], -1)
            assert sum(event["kind"] == "tool_result_received" for event in events) == 1
            # A new reader resumes after the last event without replaying commands.
            reader = WorkflowStore(pool)
            assert await reader.events(CLIENT, job["id"], events[-1]["sequence"]) == []
            assert (await reader.get(CLIENT, job["id"]))["status"] == "queued"
        finally:
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])


async def test_workflow_event_replay_paginates_in_order_without_state_changes():
    async with dedicated_pool() as pool:
        store = WorkflowStore(pool)
        job = await store.submit(CLIENT, str(uuid4()), {"synthetic": True})
        try:
            await pool.execute(
                "insert into public.workflow_events(job_id,sequence,kind) "
                "select $1,n,'synthetic' from generate_series(1,105) n", job["id"],
            )
            first = await store.events(CLIENT, job["id"], -1)
            second = await WorkflowStore(pool).events(CLIENT, job["id"], first[-1]["sequence"])
            assert [event["sequence"] for event in first + second] == list(range(106))
            assert len(first) == 100
            assert (await store.get(CLIENT, job["id"]))["version"] == 0
        finally:
            await pool.execute("delete from public.workflow_jobs where id=$1", job["id"])
