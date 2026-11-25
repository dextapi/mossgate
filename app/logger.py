"""Structured single-line JSON logging."""
import json
import logging
import sys
import time

from . import config

_log = logging.getLogger("mossgate")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(message)s"))
_log.addHandler(_handler)
_log.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))


def _emit(level, msg, **fields):
    _log.log(level, json.dumps({"ts": round(time.time(), 3), "level": logging.getLevelName(level),
                                "msg": msg, **fields}, separators=(",", ":")))


def info(msg, **fields):
    _emit(logging.INFO, msg, **fields)


def warn(msg, **fields):
    _emit(logging.WARNING, msg, **fields)


def error(msg, **fields):
    _emit(logging.ERROR, msg, **fields)


class AccessLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = {"code": 0}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            info("access", method=scope["method"], path=scope["path"], status=status["code"],
                 ms=round((time.monotonic() - started) * 1000, 1))
