"""Resource cleanup covers failed startup as well as normal lifespan shutdown."""

import asyncio
from contextlib import asynccontextmanager


class LifecycleResources:
    def __init__(self, timeout):
        self.timeout = timeout
        self.tasks = []
        self.resources = []

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def resource(self, resource):
        self.resources.append(resource)
        return resource

    async def close(self):
        for task in self.tasks:
            task.cancel()
        try:
            async with asyncio.timeout(self.timeout):
                await asyncio.gather(*self.tasks, return_exceptions=True)
                for resource in reversed(self.resources):
                    try:
                        await resource.close()
                    except Exception:
                        terminate = getattr(resource, "terminate", None)
                        if terminate:
                            terminate()
        except TimeoutError:
            for resource in self.resources:
                terminate = getattr(resource, "terminate", None)
                if terminate:
                    terminate()


def managed_lifespan(function):
    @asynccontextmanager
    async def lifespan(app):
        resources = LifecycleResources(app.state.settings.shutdown_timeout_seconds)
        app.state.lifecycle_resources = resources
        try:
            async with asynccontextmanager(function)(app):
                yield
        finally:
            app.state.ready = False
            drain = getattr(app.state, "drain", None)
            if drain is not None:
                await drain.drain(app.state.settings.shutdown_timeout_seconds)
            await resources.close()
    return lifespan
