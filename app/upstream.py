"""Upstream HTTP: pooled async client, retries, quota gating, key rotation, fallback."""
import asyncio
import random

import httpx

from . import config, keys, limiter, metrics

_client = None
_inflight = None


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(max_connections=config.MAX_CONNECTIONS,
                                max_keepalive_connections=config.MAX_CONNECTIONS // 2),
            timeout=httpx.Timeout(config.READ_TIMEOUT, connect=config.CONNECT_TIMEOUT),
            follow_redirects=True,
            headers={"user-agent": "mossgate/2"},
        )
    return _client


def semaphore():
    global _inflight
    if _inflight is None:
        _inflight = asyncio.Semaphore(config.MAX_INFLIGHT)
    return _inflight


async def aclose():
    if _client is not None:
        await _client.aclose()


class Failure(Exception):
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


async def open(method, path, make_payload, model, stream, headers, estimate):
    """Execute an upstream call. `make_payload(model)` returns (path, payload) for retries/fallback."""
    chain = [model] + catalog_fallback(model)
    last = None
    async with semaphore():
        for attempt, current in enumerate([m for m in chain for _ in range(2)]):
            gate = limiter.gate(current)
            if gate:
                try:
                    await gate.enter(estimate)
                except asyncio.TimeoutError:
                    last = Failure(429, b'{"error":{"message":"local queue timeout"}}')
                    continue
            key = await keys.get()
            if not key:
                raise Failure(503, b'{"error":{"message":"no upstream key available"}}')
            path_, payload = make_payload(current)
            auth = "x-api-key" if path_.startswith("/anthropic") else "api-key"
            try:
                req = client().build_request(method, config.UPSTREAM_BASE + path_,
                                             json=payload, headers={**headers, auth: key})
                started = metrics.now()
                resp = await client().send(req, stream=stream)
            except (httpx.HTTPError, OSError) as e:
                last = Failure(502, str(e).encode())
                await asyncio.sleep(0.3 * (attempt + 1) + random.random() / 5)
                continue
            metrics.observe_latency(model, metrics.now() - started)
            if gate:
                gate.observe(resp.headers)
            if resp.status_code == 401:
                keys.rotate(key, 60)
                last = Failure(401, await body_bytes(resp))
                await resp.aclose()
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after") or 2)
                if gate:
                    gate.penalise(retry_after)
                last = Failure(429, await body_bytes(resp), dict(resp.headers))
                await resp.aclose()
                if attempt < len(chain) * 2 - 1:
                    await asyncio.sleep(min(retry_after, 10) + random.random() / 4)
                continue
            if resp.status_code >= 500:
                last = Failure(resp.status_code, await body_bytes(resp), dict(resp.headers))
                await resp.aclose()
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            metrics.count("upstream_ok", model=current)
            return resp, current
    metrics.count("upstream_fail", model=model)
    if last:
        raise last
    raise Failure(502, b'{"error":{"message":"upstream unavailable"}}')


async def body_bytes(resp, limit=4096):
    """Read an error body even for streamed responses, bounded."""
    try:
        return (await resp.aread())[:limit]
    except httpx.HTTPError:
        return b""


def catalog_fallback(model):
    from .catalog import catalog
    return catalog.fallback(model)


async def sse_events(resp):
    """Parse an SSE response into (event, data) tuples; data is a dict or the raw string."""
    event = None
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                yield event, __import__("json").loads(raw)
            except ValueError:
                yield event, raw
            event = None
