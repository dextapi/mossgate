"""Model catalog: deployed models, aliases, capabilities and upstream quotas."""
import json
import os

from . import config


class Model:
    __slots__ = ("id", "dialect", "ctx", "max_out", "rpm", "tpm", "aliases", "created", "kind")

    def __init__(self, id, dialect, ctx, max_out, rpm, tpm, aliases=(), created=1764000000,
                 kind="chat"):
        self.kind = kind
        self.id = id
        self.dialect = dialect  # "anthropic" | "openai"
        self.ctx = ctx
        self.max_out = max_out
        self.rpm = rpm
        self.tpm = tpm
        self.aliases = list(aliases)
        self.created = created

    def to_openai(self):
        return {
            "id": self.id,
            "object": "model",
            "created": self.created,
            "owned_by": "mossgate",
            "context_window": self.ctx,
            "max_output_tokens": self.max_out,
            "x-dialect": self.dialect,
            "kind": self.kind,
        }


DEFAULT_CATALOG = [
    # Limits transcribed from upstream x-ratelimit-* headers (per 60s).
    Model("claude-opus-4-8", "anthropic", 200_000, 64_000, 40, 40_000, ("opus",)),
    Model("claude-sonnet-4-6", "anthropic", 1_000_000, 64_000, 30, 30_000, ("sonnet", "default")),
    Model("claude-haiku-5-5", "anthropic", 200_000, 64_000, 1_000, 1_000_000, ("haiku", "small")),
    Model("gpt-4.1", "openai", 1_047_576, 32_768, 500, 500_000, ("gpt",)),
    Model("model-router-2025-11-18", "openai", 400_000, 128_000, 500, 500_000,
          ("auto", "router", "gpt-5", "gpt-5.6-luna", "gpt-5.6-luna-2026-07-09")),
    Model("text-embedding-3-large", "openai", 8_191, 0, 500, 500_000,
          ("embed", "embedding", "embeddings"), kind="embedding"),
]

# Degradation order when a model is exhausted.
FALLBACK = {
    "claude-opus-4-8": ("claude-sonnet-4-6", "claude-haiku-5-5"),
    "claude-sonnet-4-6": ("claude-haiku-5-5",),
    "gpt-4.1": ("model-router-2025-11-18",),
    "model-router-2025-11-18": ("gpt-4.1",),
}

_PREFIX = [
    ("claude-opus", "claude-opus-4-8"), ("claude-sonnet", "claude-sonnet-4-6"),
    ("claude-haiku", "claude-haiku-5-5"), ("claude-fable", "claude-sonnet-4-6"),
    ("gpt-4", "gpt-4.1"), ("gpt-5", "model-router-2025-11-18"),
    ("o3", "gpt-4.1"), ("o4", "gpt-4.1"), ("gpt-", "gpt-4.1"),
]


class Catalog:
    def __init__(self, models):
        self.models = {m.id: m for m in models}
        self.alias = {a: m.id for m in models for a in m.aliases}

    @classmethod
    def load(cls, path=None):
        path = path or config.MODEL_CATALOG
        if not os.path.exists(path):
            return cls(DEFAULT_CATALOG)
        raw = json.load(open(path))
        models = [Model(**m) for m in raw.get("models", [])]
        cat = cls(models or DEFAULT_CATALOG)
        cat.alias.update(raw.get("aliases", {}))
        return cat

    def resolve(self, name):
        """Map a client model name to a deployed id; unknown names degrade by family prefix."""
        if not name:
            return "claude-sonnet-4-6"
        if name in self.models:
            return name
        if name in self.alias:
            return self.alias[name]
        for prefix, target in _PREFIX:
            if name.startswith(prefix):
                return target
        return "claude-sonnet-4-6"

    def lookup(self, name):
        """Strict: exact id or explicit alias only (for /v1/models/{id})."""
        mid = name if name in self.models else self.alias.get(name)
        return self.models.get(mid)

    def get(self, name):
        return self.lookup(self.resolve(name))

    def list(self):
        return list(self.models.values())

    def embedding_model(self, name):
        m = self.lookup(name)
        if m and m.kind == "embedding":
            return m
        return self.models.get("text-embedding-3-large")

    def fallback(self, name):
        return [m for m in FALLBACK.get(name, ()) if m in self.models]


catalog = Catalog.load()
