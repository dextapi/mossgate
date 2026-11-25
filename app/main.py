"""mossgate — OpenAI/Anthropic compatible gateway in front of an Azure AI Foundry resource."""
import time

import orjson
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import config, harvest, keys, limiter, logger, metrics, translate, upstream
from .catalog import catalog

@asynccontextmanager
async def lifespan(_):
    keys.load_static()
    if not config.KEYS and config.HARVEST_ENABLED:
        await keys.get()
    logger.info("gateway ready", upstream=config.UPSTREAM_BASE,
                models=len(catalog.models), inflight=config.MAX_INFLIGHT)
    yield
    await upstream.aclose()


app = FastAPI(title="mossgate", version="2.0.0", docs_url="/docs", openapi_url="/openapi.json",
              lifespan=lifespan)
app.add_middleware(logger.AccessLog)


def _bearer(request):
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")


def auth_required(request: Request):
    if not config.GATEWAY_TOKEN:
        return
    if _bearer(request) != config.GATEWAY_TOKEN:
        raise _Unauthorised()


def metrics_required(request: Request):
    if config.METRICS_TOKEN and _bearer(request) != config.METRICS_TOKEN:
        raise _Unauthorised()


class _Unauthorised(Exception):
    pass


@app.exception_handler(_Unauthorised)
async def _unauth(request, exc):
    return JSONResponse({"error": {"type": "authentication_error", "message": "invalid token"}}, 401)


@app.exception_handler(upstream.Failure)
async def _failure(request, exc):
    detail = exc.body[:400].decode("utf-8", "replace") if isinstance(exc.body, bytes) else str(exc.body)
    return JSONResponse({"error": {"type": "upstream_error", "status": exc.status, "message": detail}},
                        exc.status if exc.status >= 400 else 502)


@app.get("/v1/models", dependencies=[Depends(auth_required)])
async def models():
    return {"object": "list", "data": [m.to_openai() for m in catalog.list()]}


@app.get("/v1/models/{model_id}", dependencies=[Depends(auth_required)])
async def model(model_id: str):
    m = catalog.lookup(model_id)
    if not m:
        return JSONResponse({"error": {"message": f"model {model_id} not found", "type": "not_found"}}, 404)
    return m.to_openai()


@app.get("/_health")
async def health():
    return {
        "status": "ok",
        "upstream": config.UPSTREAM_BASE,
        "keys": keys.stats(),
        "quota": limiter.snapshot(),
        "models": len(catalog.models),
    }


@app.get("/metrics", dependencies=[Depends(metrics_required)])
async def prometheus():
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/_key/refresh")
async def refresh():
    found = await harvest.fetch_keys()
    for k in found:
        if k not in keys._state["keys"]:
            keys._state["keys"].append(k)
    return {"harvested": len(found), **keys.stats()}


# Betas the deployment accepts; unknown ones make the upstream reply 400.
BETAS = {"claude-code-20250219", "interleaved-thinking-2025-05-14",
         "fine-grained-tool-streaming-2025-05-14", "oauth-2025-04-20",
         "prompt-caching-2024-07-31", "structured-outputs-2025-11-13",
         "effort-2025-11-24", "context-1m-2025-08-07"}


def _passthrough_headers(request: Request):
    out = {}
    for name in ("anthropic-version", "x-request-id", "user-agent"):
        value = request.headers.get(name)
        if value:
            out[name] = value
    betas = [b.strip() for b in request.headers.get("anthropic-beta", "").split(",") if b.strip()]
    kept = [b for b in betas if b in BETAS]
    if kept:
        out["anthropic-beta"] = ",".join(kept)
    out.setdefault("anthropic-version", "2023-06-01")
    return out


def _relay_headers(resp):
    out = {"cache-control": "no-cache", "x-accel-buffering": "no"}
    for name in ("x-ratelimit-remaining-requests", "x-ratelimit-limit-requests",
                 "x-ratelimit-remaining-tokens", "x-ratelimit-limit-tokens",
                 "x-ms-region", "apim-request-id"):
        value = resp.headers.get(name)
        if value:
            out[name] = value
    return out


async def _stream(gen, resp, dialect):
    try:
        async for chunk in gen:
            yield chunk.encode() if isinstance(chunk, str) else chunk
    finally:
        await resp.aclose()


@app.post("/v1/chat/completions", dependencies=[Depends(auth_required)])
async def chat(request: Request):
    body = orjson.loads(await request.body() or b"{}")
    model_id = catalog.resolve(body.get("model"))
    spec = catalog.models[model_id]
    stream = bool(body.get("stream"))
    headers = _passthrough_headers(request)
    estimate = translate.estimate_tokens(body)
    metrics.count("requests", endpoint="chat", model=model_id)

    if spec.dialect == "openai":
        def payload(m):
            body["model"] = m
            return "/openai/v1/chat/completions", body
    else:
        converted = translate.openai_to_anthropic(body)
        converted["stream"] = stream

        def payload(m):
            converted["model"] = m
            return "/anthropic/v1/messages", converted

    resp, served = await upstream.open("POST", None, payload, model_id, stream, headers, estimate)
    if served != model_id:
        metrics.count("fallback", from_=model_id, to=served)

    if stream:
        if spec.dialect == "openai":
            gen = _raw_lines(resp)
        else:
            gen = translate.anthropic_stream_to_openai(upstream.sse_events(resp), served)
        return StreamingResponse(_stream(gen, resp, "openai"), status_code=resp.status_code,
                                 media_type="text/event-stream", headers=_relay_headers(resp))

    data = orjson.loads(await resp.aread())
    await resp.aclose()
    metrics.count("tokens", n=(data.get("usage") or {}).get("prompt_tokens", 0) or 0, kind="in")
    if spec.dialect == "anthropic":
        data = translate.anthropic_to_openai_response(data, served)
    return JSONResponse(data, status_code=resp.status_code, headers=_relay_headers(resp))


async def _raw_lines(resp):
    async for line in resp.aiter_lines():
        yield line + "\n"


@app.post("/v1/messages", dependencies=[Depends(auth_required)])
async def messages(request: Request):
    body = orjson.loads(await request.body() or b"{}")
    model_id = catalog.resolve(body.get("model"))
    spec = catalog.models[model_id]
    stream = bool(body.get("stream"))
    headers = _passthrough_headers(request)
    estimate = translate.estimate_tokens(body)
    metrics.count("requests", endpoint="messages", model=model_id)
    logger.info("msg_request", model=body.get("model"), stream=stream,
                head=orjson.dumps(body)[:400].decode())

    if spec.dialect == "anthropic":
        # Hoist system-role messages, which the Azure deployment rejects inside `messages`.
        sys_msgs = [m.get("content") for m in body.get("messages", []) if m.get("role") == "system"]
        if sys_msgs:
            body["messages"] = [m for m in body["messages"] if m.get("role") != "system"]
            text = "\n\n".join(c if isinstance(c, str) else str(c) for c in sys_msgs)
            body["system"] = f"{body['system']}\n\n{text}" if body.get("system") else text

        def payload(m):
            body["model"] = m
            return "/anthropic/v1/messages", body
    else:
        converted = translate.anthropic_to_openai_request(body)
        converted["stream"] = stream

        def payload(m):
            converted["model"] = m
            return "/openai/v1/chat/completions", converted

    resp, served = await upstream.open("POST", None, payload, model_id, stream, headers, estimate)
    if served != model_id:
        metrics.count("fallback", from_=model_id, to=served)

    if stream:
        if spec.dialect == "anthropic":
            gen = _raw_lines(resp)
        else:
            async def _events():
                async for _, data in upstream.sse_events(resp):
                    if data == "[DONE]":
                        yield None, data
                    else:
                        yield None, data
            gen = translate.openai_stream_to_anthropic(_events(), served)
        return StreamingResponse(_stream(gen, resp, "anthropic"), status_code=resp.status_code,
                                 media_type="text/event-stream", headers=_relay_headers(resp))

    data = orjson.loads(await resp.aread())
    await resp.aclose()
    if spec.dialect == "openai":
        data = translate.openai_to_anthropic_response(data, served)
    logger.info("msg_response", status=resp.status_code, head=orjson.dumps(data)[:400].decode())
    return JSONResponse(data, status_code=resp.status_code, headers=_relay_headers(resp))


@app.get("/")
async def root():
    return {"name": "mossgate", "models": "/v1/models", "health": "/_health",
            "time": int(time.time())}
