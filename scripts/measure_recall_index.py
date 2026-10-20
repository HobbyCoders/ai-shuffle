"""CPU-only Recall index lab; synthetic text, no store, model or session data.

Run: python scripts/measure_recall_index.py --repeats 10
Reports uncached vs warm-index scoring over 241/450 facts and verifies scores.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fluid"))
from fluid.codemode import relevance as matcher

QUERIES = ("worker", "deploy", "skill", "store", "hub", "UI", "pytest", "C++", "C#", "テスト", "तैयार", "192.0.2.10")


def corpus(count, mixed):
    tail = " 日本語のテスト設定。 यह सर्वर तैयार है। ทดสอบทุกวัน។" if mixed else ""
    return [f"Fixture {i}: workers deploy with FactStore and GitHub. UI helpers use pytest, C++ and C#. "
            f"The local endpoint is 192.0.2.{i % 256}; keep service{i} isolated. "
            + "Stable build conventions and deployment checks. " * 3 + tail for i in range(count)]


def sample(texts, queries, repeats, *, prepared=True):
    elapsed, scores = [], []
    for _ in range(repeats):
        for terms in queries:
            start = time.perf_counter()
            score = matcher.recall_scorer(terms) if prepared else lambda text: matcher.recall_relevance(text, terms)
            scores = [score(text) for text in texts]
            elapsed.append((time.perf_counter() - start) * 1000)
    ordered = sorted(elapsed)
    return {"median_ms": round(statistics.median(elapsed), 4),
            "p75_ms": round(ordered[(len(ordered) - 1) * 3 // 4], 4)}


def run(repeats):
    cache = getattr(matcher, "_recall_index", None)
    result = {"module_sha256": hashlib.sha256(Path(matcher.__file__).read_bytes()).hexdigest(),
              "has_index_cache": cache is not None, "repeats": repeats, "cases": {}}
    for name, count, mixed, queries in (
        ("ascii_241", 241, False, [matcher.recall_terms(q) for q in QUERIES]),
        ("mixed_241", 241, True, [matcher.recall_terms(q) for q in QUERIES]),
        ("terms64_facts450", 450, True, [frozenset(f"keyword{i}" for i in range(64))]),
    ):
        texts = corpus(count, mixed)
        if cache is None:
            result["cases"][name] = {"facts": count, "queries": len(queries),
                                      "uncached": sample(texts, queries, repeats)}
            continue
        with patch.object(matcher, "_recall_index", cache.__wrapped__):
            expected = [[matcher.recall_relevance(t, q) for t in texts] for q in queries]
            before = sample(texts, queries, repeats)
        cache.cache_clear()
        actual = [[matcher.recall_relevance(t, q) for t in texts] for q in queries]
        if actual != expected:
            raise AssertionError("Cached index changed relevance scores")
        after = sample(texts, queries, repeats)
        per_fact = sample(texts, queries, repeats, prepared=False)
        info = cache.cache_info()
        result["cases"][name] = {"facts": count, "queries": len(queries), "scores_equal": True,
                                  "uncached": before, "warm_cached": after,
                                  "warm_per_fact_classification": per_fact,
                                  "cache_hits": info.hits, "cache_misses": info.misses,
                                  "cache_maxsize": info.maxsize}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    print(json.dumps(run(args.repeats), indent=2))
