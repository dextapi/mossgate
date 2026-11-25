"""Per-model admission control: request and token buckets sized to the upstream quota.

Staying just under the published limit turns 429s into short waits instead of failures.
Buckets resync from upstream x-ratelimit-remaining-* headers when adaptive mode is on.
"""
import asyncio
import time

from . import config


class Bucket:
    __slots__ = ("capacity", "rate", "level", "updated", "cond")

    def __init__(self, capacity):
        self.capacity = max(1.0, capacity * config.QUOTA_RATIO)
        self.rate = self.capacity / 60.0
        self.level = self.capacity
        self.updated = time.monotonic()
        self.cond = asyncio.Condition()

    def _refill(self):
        now = time.monotonic()
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now

    async def acquire(self, amount=1.0, timeout=None):
        async with self.cond:
            deadline = time.monotonic() + timeout if timeout else None
            while True:
                self._refill()
                if self.level >= amount:
                    self.level -= amount
                    return
                wait = (amount - self.level) / self.rate
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    wait = min(wait, remaining)
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=min(wait, 5.0))
                except asyncio.TimeoutError:
                    pass

    def sync(self, remaining):
        """Align the bucket with upstream truth (never raises the level above capacity)."""
        if not config.ADAPTIVE_QUOTA or remaining is None:
            return
        self._refill()
        self.level = min(self.capacity, max(0.0, remaining * config.QUOTA_RATIO))

    def hold(self, seconds):
        self.level = 0.0
        self.updated = time.monotonic() + seconds


class Gate:
    def __init__(self, model):
        self.model = model
        self.req = Bucket(model.rpm)
        self.tok = Bucket(model.tpm)

    async def enter(self, est_tokens):
        await self.req.acquire(1, timeout=config.QUEUE_TIMEOUT)
        await self.tok.acquire(min(est_tokens, self.tok.capacity / 2), timeout=config.QUEUE_TIMEOUT)

    def observe(self, headers):
        self.req.sync(_num(headers.get("x-ratelimit-remaining-requests")))
        self.tok.sync(_num(headers.get("x-ratelimit-remaining-tokens")))

    def penalise(self, retry_after):
        self.req.hold(retry_after)
        self.tok.hold(retry_after)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_gates = {}


def gate(model_id):
    g = _gates.get(model_id)
    if g is None:
        from .catalog import catalog
        m = catalog.models.get(model_id)
        if m is None:
            return None
        g = _gates[model_id] = Gate(m)
    return g


def snapshot():
    out = {}
    for mid, g in _gates.items():
        g.req._refill()
        g.tok._refill()
        out[mid] = {"req": round(g.req.level, 1), "req_cap": round(g.req.capacity, 1),
                    "tok": round(g.tok.level, 0), "tok_cap": round(g.tok.capacity, 0)}
    return out
