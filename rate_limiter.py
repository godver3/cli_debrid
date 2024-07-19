
import asyncio

class RateLimiter:
    def __init__(self, rate_limit):
        self.rate_limit = rate_limit
        self.tokens = rate_limit
        self.last_refill = asyncio.get_event_loop().time()

    async def acquire(self):
        while self.tokens < 1:
            await self.refill()
            await asyncio.sleep(0.1)
        self.tokens -= 1

    async def refill(self):
        now = asyncio.get_event_loop().time()
        time_since_refill = now - self.last_refill
        new_tokens = time_since_refill * self.rate_limit
        self.tokens = min(self.tokens + new_tokens, self.rate_limit)
        self.last_refill = now
