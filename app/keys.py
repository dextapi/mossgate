"""Key pool: rotation, cooldown on 401 and single-flight re-harvest."""
import asyncio
import itertools
import os
import time

from . import config, harvest

KEY_FILE = config.KEY_FILE

_lock = asyncio.Lock()
_state = {"keys": [], "cool": {}, "last": 0.0, "rr": itertools.count()}


def load_static():
    """Env keys win; otherwise adopt keys persisted by a previous harvest."""
    found = list(config.KEYS)
    if not found and os.path.exists(KEY_FILE):
        for line in open(KEY_FILE):
            if line.startswith("ANTHROPIC_API_KEY="):
                found.append(line.split("=", 1)[1].strip())
    _state["keys"] = [k for k in found if k]
    _state["last"] = 0.0  # 0 => the first get() may harvest immediately


def healthy():
    now = time.time()
    return [k for k in _state["keys"] if _state["cool"].get(k, 0) <= now]


async def get():
    """Return a usable upstream key, refreshing from the container when the pool runs dry."""
    keys = healthy()
    if keys:
        return keys[next(_state["rr"]) % len(keys)]
    async with _lock:
        keys = healthy()
        if keys:
            return keys[next(_state["rr"]) % len(keys)]
        if not config.HARVEST_ENABLED:
            return None
        if time.time() - _state["last"] < config.HARVEST_MIN_INTERVAL:
            return None
        _state["last"] = time.time()
        found = await harvest.fetch_keys()
        for k in found:
            if k not in _state["keys"]:
                _state["keys"].append(k)
        _state["cool"] = {k: 0 for k in _state["keys"]}
        return healthy()[0] if healthy() else None


def rotate(key, cooldown=90):
    _state["cool"][key] = time.time() + cooldown


def stats():
    return {"keys": len(_state["keys"]), "healthy": len(healthy()),
            "fingerprints": [k[-8:] for k in _state["keys"]]}
