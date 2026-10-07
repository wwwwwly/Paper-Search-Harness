"""Share an in-flight read; cancel it when the last owner leaves."""

import asyncio


class SingleFlight:
    def __init__(self):
        self.tasks = {}
        self.waiters = {}

    async def run(self, key, read):
        if key not in self.tasks:
            self.tasks[key] = asyncio.create_task(read())
            self.waiters[key] = 0
        task = self.tasks[key]
        self.waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            self.waiters[key] -= 1
            if self.waiters[key] == 0:
                self.tasks.pop(key)
                self.waiters.pop(key)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
