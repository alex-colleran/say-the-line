"""A timing log, so caching goes where it helps: how long each request and each job step takes, and
whether a cache already had what was asked for.

data/perf.jsonl gets one JSON object per line:
  {"t", "ev": "req", "m": method, "r": route ("/thumb/#/#.jpg"), "sig", "code", "ms", "bytes", "cache": [[name, hit]]}
  {"t", "ev": "job", "kind", "key", "sig", "units", "state", "secs", "wait", "phases": {state: s}, "err"?}
sig is a short hash of the exact request (path and query, or the job's body): the same sig twice
is work a cache could have saved, which summary() counts as repeats.
  {"t", "ev": "cache", "cache", "hit"}                    (a cache looked up outside any request)
Past MAX_BYTES the file rolls to perf.1.jsonl (one old file kept), so it never holds more than
about 2 x MAX_BYTES. summary() (GET /api/perf, or python -m saytheline.perf) reads both."""
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict

from . import config

PATH = os.environ.get("SAYTHELINE_PERF") or os.path.join(config.data_dir(), "perf.jsonl")
OLD = PATH[:-len(".jsonl")] + ".1.jsonl"
MAX_BYTES = 10 * 1024 * 1024
_lock = threading.Lock()
_local = threading.local()


def write(rec):
    line = json.dumps({"t": round(time.time(), 3), **rec}, separators=(",", ":")) + "\n"
    with _lock:
        try:
            if os.path.exists(PATH) and os.path.getsize(PATH) > MAX_BYTES:
                os.replace(PATH, OLD)
            with open(PATH, "a") as f:
                f.write(line)
        except OSError:
            pass                      # the log is never worth failing a request over


def begin():
    """Start collecting cache lookups for the request on this thread."""
    _local.marks = []


def end():
    marks, _local.marks = getattr(_local, "marks", None), None
    return marks or []


def cache(name, hit):
    """Note a cache lookup: on the current request's line, or as its own line (inside a job)."""
    marks = getattr(_local, "marks", None)
    if marks is not None:
        marks.append([name, bool(hit)])
    else:
        write({"ev": "cache", "cache": name, "hit": bool(hit)})


def route(path):
    """A path with its ids and names taken out, so like requests group together."""
    for pre in ("/clips/", "/uploads/", "/interp/"):
        if path.startswith(pre):
            return pre + "*"
    return re.sub(r"(?<![A-Za-z])\d+", "#", path)


def sig(x):
    return hashlib.sha1(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _repeats(rs):
    return len(rs) - len({r.get("sig") for r in rs}) if all(r.get("sig") for r in rs) else None


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def summary(days=7):
    """Per route: count, median / 95th percentile / slowest ms, average size and cache hit rates;
    per cache: hits and misses; per job key: count, average seconds and average per step; the
    slowest requests."""
    since = time.time() - days * 86400
    routes, caches, jobs, slow = defaultdict(list), defaultdict(lambda: [0, 0]), defaultdict(list), []
    for p in (OLD, PATH):
        try:
            f = open(p)
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) < since:
                    continue
                if r.get("ev") == "req":
                    routes[f'{r["m"]} {r["r"]}'].append(r)
                    for name, hit in r.get("cache") or []:
                        caches[name][0 if hit else 1] += 1
                    slow.append(r)
                elif r.get("ev") == "cache":
                    caches[r["cache"]][0 if r["hit"] else 1] += 1
                elif r.get("ev") == "job":
                    jobs[r.get("key") or r.get("kind")].append(r)
    out_routes = []
    for k, rs in routes.items():
        ms = [r["ms"] for r in rs]
        hits = defaultdict(lambda: [0, 0])
        for r in rs:
            for name, hit in r.get("cache") or []:
                hits[name][0 if hit else 1] += 1
        out_routes.append(dict(route=k, n=len(rs), repeats=_repeats(rs), p50_ms=_pct(ms, 0.5), p95_ms=_pct(ms, 0.95), max_ms=max(ms),
                               total_s=round(sum(ms) / 1000, 1), avg_kb=round(sum(r.get("bytes") or 0 for r in rs) / len(rs) / 1024, 1),
                               cache={n: dict(hit=h, miss=m) for n, (h, m) in hits.items()}))
    out_routes.sort(key=lambda r: -r["total_s"])                  # where the waiting adds up
    out_jobs = []
    for k, rs in jobs.items():
        steps = defaultdict(list)
        for r in rs:
            for s, v in (r.get("phases") or {}).items():
                steps[s].append(v)
        out_jobs.append(dict(key=k, n=len(rs), repeats=_repeats(rs), avg_s=round(sum(r["secs"] for r in rs) / len(rs), 1),
                             avg_wait_s=round(sum(r.get("wait") or 0 for r in rs) / len(rs), 1),
                             failed=sum(r["state"] != "done" for r in rs),
                             steps={s: round(sum(v) / len(v), 1) for s, v in steps.items()}))
    out_jobs.sort(key=lambda r: -r["avg_s"] * r["n"])
    return dict(days=days, routes=out_routes,
                caches={n: dict(hit=h, miss=m, rate=round(h / (h + m), 2) if h + m else None) for n, (h, m) in caches.items()},
                jobs=out_jobs,
                slowest=[dict(route=r["r"], ms=r["ms"], at=time.strftime("%m-%d %H:%M", time.localtime(r["t"])))
                         for r in sorted(slow, key=lambda r: -r["ms"])[:15]])


if __name__ == "__main__":
    json.dump(summary(float(sys.argv[1]) if len(sys.argv) > 1 else 7), sys.stdout, indent=1)
    print()
