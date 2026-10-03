"""Role-specific worker loops using the same configuration lifecycle as the API."""

import asyncio
import logging
import signal

from gateway.distributed_capacity import CapacityUnavailable, DistributedCapacity
from gateway.logging import log_event
from gateway.workflow_worker import WorkflowWorker

logger = logging.getLogger("gateway.worker")


async def workflow_loop(app):
    while True:
        if app.state.drain.draining:
            return
        runtime = app.state.runtime
        if runtime is None:
            await asyncio.sleep(1)
            continue
        capacity = DistributedCapacity(app.state.db_pool)
        try:
            owner = await capacity.acquire({
                "workflow:global": app.state.settings.workflow_global_capacity,
            })
        except CapacityUnavailable:
            await asyncio.sleep(1)
            continue
        if not app.state.drain.enter():
            await capacity.release(owner)
            return
        worker = WorkflowWorker(app.state.db_pool, runtime, app.state.settings,
                                live_state=app.state.live_state,
                                health_recorder=app.state.health_recorder)
        tick = asyncio.create_task(worker.run_once())
        pulse = capacity.guard(owner, 1, tick)
        try:
            worked = await tick
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            worked = False  # Lost capacity cancels the tick, not the service process.
        except Exception as exc:
            log_event(logger, logging.ERROR, "workflow_tick_failed", error_type=type(exc).__name__)
            worked = False
        finally:
            try:
                pulse.cancel()
                tick.cancel()
                await asyncio.gather(pulse, tick, return_exceptions=True)
                try:
                    async with asyncio.timeout(2):
                        await capacity.release(owner)
                except Exception:
                    log_event(logger, logging.WARNING, "workflow_capacity_release_deferred")
            finally:
                app.state.drain.leave()
        if not worked:
            await asyncio.sleep(1)


async def serve_role(settings):
    # Imported after explicit development bootstrap/settings selection.
    from gateway.app import create_app

    app = create_app(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with app.router.lifespan_context(app):
        worker = (
            asyncio.create_task(workflow_loop(app)) if settings.service_role == "workflow" else None
        )
        waiter = asyncio.create_task(stop.wait())
        try:
            if worker:
                done, _ = await asyncio.wait({worker, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if worker in done:
                    await worker
            else:
                await waiter
        finally:
            waiter.cancel()
            if worker:
                await app.state.drain.drain(settings.shutdown_timeout_seconds)
                worker.cancel()
                try:
                    async with asyncio.timeout(settings.shutdown_timeout_seconds):
                        await asyncio.gather(worker, return_exceptions=True)
                except TimeoutError:
                    pass
            await asyncio.gather(waiter, return_exceptions=True)
