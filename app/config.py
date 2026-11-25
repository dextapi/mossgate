"""Runtime configuration, all overridable by environment."""
import os

def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 8787)

# Upstream Azure AI Foundry resource. Paths are appended by the router.
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://magichal.services.ai.azure.com").rstrip("/")
if UPSTREAM_BASE.endswith("/anthropic"):
    UPSTREAM_BASE = UPSTREAM_BASE[: -len("/anthropic")]

# Static keys (comma separated). Empty -> harvest from the MOSS dev container.
KEYS = [k.strip() for k in os.environ.get("MOSSGATE_KEYS", "").split(",") if k.strip()]
HARVEST_ENABLED = os.environ.get("HARVEST", "1") not in ("0", "false", "no")
HARVEST_URL = os.environ.get(
    "HARVEST_URL",
    "https://pythiav2-dev.purplesmoke-ff24e48e.uksouth.azurecontainerapps.io/api/data/query",
)
HARVEST_MIN_INTERVAL = _int("HARVEST_MIN_INTERVAL", 60)

# Client auth. Unset -> open. /_health and /metrics stay open unless METRICS_TOKEN set.
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")

# Concurrency / backpressure.
MAX_INFLIGHT = _int("MAX_INFLIGHT", 256)
MAX_CONNECTIONS = _int("MAX_CONNECTIONS", 200)
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "15"))
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "600"))
MAX_RETRIES = _int("MAX_RETRIES", 3)
QUEUE_TIMEOUT = float(os.environ.get("QUEUE_TIMEOUT", "120"))

# Admission control. Multiplies the known upstream quota to stay just under it.
QUOTA_RATIO = float(os.environ.get("QUOTA_RATIO", "0.95"))
# Learn remaining quota from upstream headers.
ADAPTIVE_QUOTA = os.environ.get("ADAPTIVE_QUOTA", "1") not in ("0", "false", "no")

MODEL_CATALOG = os.environ.get("MODEL_CATALOG", "models.json")
# Where harvested credentials persist (mounted volume in containers).
KEY_FILE = os.environ.get("KEY_FILE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "key.env")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
