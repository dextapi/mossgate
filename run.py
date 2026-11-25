#!/usr/bin/env python3
"""Serve the gateway. `python run.py` or `uvicorn app.main:app`."""
import uvicorn

from app import config, logger

if __name__ == "__main__":
    logger.info("starting", host=config.HOST, port=config.PORT,
                harvest=config.HARVEST_ENABLED, auth=bool(config.GATEWAY_TOKEN))
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_config=None,
                access_log=False, workers=1)
