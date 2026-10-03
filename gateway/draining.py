"""Per-process admission draining for rolling restarts."""

import asyncio


class DrainState:
    def __init__(self):
        self.draining = False
        self.active = 0
        self.empty = asyncio.Event()
        self.empty.set()

    def enter(self):
        if self.draining:
            return False
        self.active += 1
        self.empty.clear()
        return True

    def leave(self):
        self.active -= 1
        if self.active == 0:
            self.empty.set()

    async def drain(self, timeout):
        self.draining = True
        try:
            async with asyncio.timeout(timeout):
                await self.empty.wait()
            return True
        except TimeoutError:
            return False
