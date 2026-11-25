"""Minimal in-process metrics: counters and latency buckets, Prometheus text format."""
import time
from collections import defaultdict

_counters = defaultdict(int)
_latency = defaultdict(lambda: [0.0, 0])
_BUCKETS = (0.1, 0.3, 1, 3, 10, 30, 120)
_hist = defaultdict(lambda: [0] * (len(_BUCKETS) + 1))
_started = time.time()

now = time.monotonic


def count(name, n=1, **labels):
    suffix = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    _counters[f"{name}{{{suffix}}}" if suffix else name] += n


def observe_latency(model, seconds):
    _latency[model][0] += seconds
    _latency[model][1] += 1
    for i, edge in enumerate(_BUCKETS):
        if seconds <= edge:
            _hist[model][i] += 1
            break
    else:
        _hist[model][len(_BUCKETS)] += 1


def render():
    lines = ["# TYPE mossgate_uptime_seconds gauge",
             f"mossgate_uptime_seconds {time.time() - _started:.0f}"]
    for name, value in _counters.items():
        metric = name.split("{")[0]
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{name} {value}")
    for model, (total, n) in _latency.items():
        lines.append(f'mossgate_upstream_latency_seconds_sum{{model="{model}"}} {total:.4f}')
        lines.append(f'mossgate_upstream_latency_seconds_count{{model="{model}"}} {n}')
        for i, edge in enumerate(_BUCKETS):
            lines.append(f'mossgate_upstream_latency_seconds_bucket{{model="{model}",le="{edge}"}} '
                         f'{_hist[model][i]}')
    return "\n".join(lines) + "\n"
