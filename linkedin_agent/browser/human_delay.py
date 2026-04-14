import asyncio
import random

async def delay(min_ms: int = 800, max_ms: int = 2400):
    """Wait a random human-like duration between min_ms and max_ms milliseconds."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)
