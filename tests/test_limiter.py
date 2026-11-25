import asyncio

import pytest

from app import config, limiter
from app.catalog import FALLBACK, catalog


def test_catalog_resolution():
    assert catalog.resolve("opus") == "claude-opus-4-8"
    assert catalog.resolve("claude-sonnet-5-5") == "claude-sonnet-4-6"
    assert catalog.resolve("gpt-5.3-turbo") == "model-router-2025-11-18"
    assert catalog.resolve("totally-unknown") == "claude-sonnet-4-6"
    assert catalog.resolve(None) == "claude-sonnet-4-6"


def test_catalog_fallback_chains_exist():
    for model, chain in FALLBACK.items():
        assert model in catalog.models
        assert all(m in catalog.models for m in chain)


@pytest.mark.asyncio
async def test_gate_queues_until_capacity(monkeypatch):
    monkeypatch.setattr(config, "QUOTA_RATIO", 1.0)
    g = limiter.Gate(catalog.models["claude-opus-4-8"])
    g.req.capacity = 2.0
    g.req.level = 2.0
    g.req.rate = 10.0
    g.tok.capacity = 1e9
    g.tok.level = 1e9
    await g.enter(10)
    await g.enter(10)
    task = asyncio.create_task(g.enter(10))
    await asyncio.sleep(0.01)
    assert not task.done()
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_gate_times_out_when_exhausted(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_TIMEOUT", 0.5)
    monkeypatch.setattr(config, "QUOTA_RATIO", 1.0)
    g = limiter.Gate(catalog.models["claude-opus-4-8"])
    g.req.capacity = 1.0
    g.req.level = 0.0
    g.req.rate = 0.01
    g.tok.capacity = 1e9
    g.tok.level = 1e9
    with pytest.raises(asyncio.TimeoutError):
        await g.enter(10)


def test_sync_never_exceeds_capacity(monkeypatch):
    monkeypatch.setattr(config, "ADAPTIVE_QUOTA", True)
    g = limiter.Gate(catalog.models["claude-haiku-5-5"])
    g.req.sync(10_000)
    assert g.req.level <= g.req.capacity
    g.req.sync(1)
    assert 0 <= g.req.level <= 2
